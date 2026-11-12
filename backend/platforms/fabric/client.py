"""
Fabric's REST APIs as the person (workspaces and items, Power BI DAX queries), and the
pool that reuses warehouse connections between queries. Token scopes live in auth.py.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.core.dynamic_registry.base import AdapterError, CredentialRejected, Rows

FABRIC_API = "https://api.fabric.microsoft.com/v1"
_MAX_PAGES = 20


def _explain(response: httpx.Response) -> str:
    if response.status_code == 401:
        return "Fabric rejected the sign-in (401). Link your Microsoft account again."
    if response.status_code == 403:
        return "You don't have access to this Fabric item (403)."
    if response.status_code == 404:
        return "That workspace or warehouse doesn't exist or isn't visible to you (404)."
    return f"Fabric answered HTTP {response.status_code}."


class FabricApi:
    def __init__(self, access_token: str, http: httpx.Client | None = None) -> None:
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._http = http or httpx.Client(timeout=30.0)

    def _get(self, url: str) -> dict[str, Any]:
        response = self._http.get(url, headers=self._headers)
        if response.status_code != 200:
            raise AdapterError(_explain(response))
        return response.json()

    def _paged(self, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        for _ in range(_MAX_PAGES):
            if not next_url:
                break
            page = self._get(next_url)
            items.extend(page.get("value") or [])
            next_url = page.get("continuationUri")
        return items

    def workspaces(self) -> list[dict[str, str]]:
        found = [{"id": w["id"], "name": w.get("displayName", "")}
                 for w in self._paged(f"{FABRIC_API}/workspaces") if w.get("id")]
        return sorted(found, key=lambda w: w["name"].lower())

    def semantic_models(self, workspace_id: str) -> list[dict[str, str]]:
        found = [{"id": m["id"], "name": m.get("displayName", "")}
                 for m in self._paged(f"{FABRIC_API}/workspaces/{workspace_id}/semanticModels")
                 if m.get("id")]
        return sorted(found, key=lambda m: m["name"].lower())

    def warehouses(self, workspace_id: str) -> list[dict[str, str]]:
        found = []
        for item in self._paged(f"{FABRIC_API}/workspaces/{workspace_id}/warehouses"):
            if not item.get("id"):
                continue
            found.append({
                "id": item["id"],
                # A Fabric warehouse's SQL database name is its display name.
                "name": item.get("displayName", ""),
                "sql_endpoint": (item.get("properties") or {}).get("connectionString", ""),
            })
        return sorted(found, key=lambda w: w["name"].lower())


POWERBI_API = "https://api.powerbi.com/v1.0/myorg"


def _explain_powerbi(response: httpx.Response) -> str:
    try:
        detail = response.json().get("error", {})
        message = detail.get("pbi.error", {}).get("details", [{}])[0].get("detail", {}).get(
            "value") or detail.get("message") or ""
    except (ValueError, AttributeError, IndexError):
        message = ""
    if response.status_code == 403:
        return ("Power BI refused (403): you may lack Build permission on this semantic model, "
                "or the tenant blocks the Execute Queries API."
                + (f" {message}" if message else ""))
    if response.status_code == 404:
        return "That semantic model doesn't exist or isn't visible to you (404)."
    return f"Power BI answered HTTP {response.status_code}." + (f" {message}" if message else "")


def execute_dax(http: httpx.Client, token: str, workspace_id: str, model_id: str,
                dax: str) -> Rows:
    try:
        response = http.post(
            f"{POWERBI_API}/groups/{workspace_id}/datasets/{model_id}/executeQueries",
            headers={"Authorization": f"Bearer {token}"},
            json={"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}},
        )
    except httpx.HTTPError as error:
        raise AdapterError(f"Could not reach Power BI: {type(error).__name__}") from None
    if response.status_code == 401:
        raise CredentialRejected("Power BI rejected the sign-in (401).")
    if response.status_code != 200:
        raise AdapterError(_explain_powerbi(response))
    body: dict[str, Any] = response.json()
    result = (body.get("results") or [{}])[0]
    if result.get("error"):
        raise AdapterError(f"DAX: {result['error'].get('message', 'the query failed')}")
    rows = ((result.get("tables") or [{}])[0]).get("rows") or []
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return Rows(columns, [tuple(row.get(c) for c in columns) for row in rows])


# --- reusing warehouse connections --------------------------------------------------
# Reusing Fabric warehouse connections between queries.
#
# Opening one costs a TLS handshake plus Entra authentication at the SQL
# endpoint, which a conversation of many short queries would otherwise pay
# every time. The rules that make reuse safe here:
#
#   * A connection is only reused for the same connection id AND the same access
#     token it was opened with (the key holds a fingerprint of the token, never
#     the token). A new token -- hourly, or after relinking -- means a new
#     connection, so access revoked in Entra is not kept alive by an old socket.
#   * No connection lives longer than `max_age` (below an access token's
#     lifetime) or sits idle longer than `idle`.
#   * A connection serves one query at a time: it is taken out of the pool while
#     in use, and a second simultaneous query opens its own.
#   * A connection that raised is closed, never returned.

log = logging.getLogger(__name__)

Key = tuple[str, str, str, str]  # connection id, endpoint, database, token fingerprint


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:24]


@dataclass
class _Idle:
    connection: Any
    opened: float
    returned: float = field(default=0.0)


class FabricConnectionPool:
    def __init__(self, *, idle_seconds: float, max_age_seconds: float,
                 max_idle_per_key: int = 2, max_idle_total: int = 200,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._idle_seconds = idle_seconds
        self._max_age = max_age_seconds
        self._per_key = max_idle_per_key
        self._total = max_idle_total
        self._clock = clock
        self._idle: dict[Key, list[_Idle]] = {}
        self._lock = threading.Lock()

    def now(self) -> float:
        """The pool's clock, used to stamp when a connection was opened."""
        return self._clock()

    def take(self, key: Key) -> tuple[Any, float] | None:
        """An idle, still-young connection for this key, or None."""
        expired: list[Any] = []
        try:
            with self._lock:
                self._sweep(expired)
                stack = self._idle.get(key)
                if not stack:
                    return None
                entry = stack.pop()
                if not stack:
                    del self._idle[key]
                return entry.connection, entry.opened
        finally:
            _close_all(expired)

    def give_back(self, key: Key, connection: Any, opened: float) -> None:
        """Return a healthy connection after use (it may be closed instead)."""
        now = self._clock()
        keep = now - opened < self._max_age
        surplus: list[Any] = []
        with self._lock:
            stack = self._idle.setdefault(key, [])
            if keep and len(stack) < self._per_key:
                stack.append(_Idle(connection, opened, now))
                while sum(len(s) for s in self._idle.values()) > self._total:
                    surplus.append(self._evict_oldest())
            else:
                surplus.append(connection)
                if not stack:
                    del self._idle[key]
        _close_all(surplus)

    def close_all(self) -> None:
        with self._lock:
            everything = [e.connection for stack in self._idle.values() for e in stack]
            self._idle.clear()
        _close_all(everything)

    def idle_count(self) -> int:
        with self._lock:
            return sum(len(stack) for stack in self._idle.values())

    # Caller holds the lock for both helpers below.

    def _sweep(self, expired: list[Any]) -> None:
        now = self._clock()
        for key in list(self._idle):
            fresh = []
            for entry in self._idle[key]:
                too_idle = now - entry.returned >= self._idle_seconds
                too_old = now - entry.opened >= self._max_age
                if too_idle or too_old:
                    expired.append(entry.connection)
                else:
                    fresh.append(entry)
            if fresh:
                self._idle[key] = fresh
            else:
                del self._idle[key]

    def _evict_oldest(self) -> Any:
        key, index = min(
            ((k, i) for k, stack in self._idle.items() for i in range(len(stack))),
            key=lambda ki: self._idle[ki[0]][ki[1]].returned,
        )
        entry = self._idle[key].pop(index)
        if not self._idle[key]:
            del self._idle[key]
        return entry.connection


def _close_all(connections: list[Any]) -> None:
    for connection in connections:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing a dead connection
            log.debug("Closing a pooled Fabric connection failed", exc_info=True)
