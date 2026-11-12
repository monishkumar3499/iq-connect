"""
Snowflake, queried as the person: their own OAuth access token (Snowflake OAuth, or
an Entra token through External OAuth) on Snowflake's SQL API, in one role. Grants,
row access policies and masking apply exactly as they do to them.

Connection shape:
  settings: {"warehouse": <name>, "role": <role or "">}   the warehouse queries run on
  scope:    {"catalog": <database>, "schemas": [...]}

Browsing uses SHOW commands, which need no running warehouse; queries run on the
chosen warehouse with the connection's database and default schema set, so
unqualified names resolve exactly as the SQL policy assumed when it checked them.
A query still running after the wait keeps running and is fetched later by its
statement handle (start_query, fetch_result).

No Snowflake password or key is ever stored or used: only the person's token.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from backend.core.dynamic_registry.base import (
    AdapterError,
    CredentialRejected,
)

# The SQL API answers within this many seconds, or hands back a statement handle.
MAX_WAIT_SECONDS = 45
_POLL_SECONDS = 1.0
_SYSTEM_DATABASES = {"snowflake"}


def quote_ident(name: str) -> str:
    """A Snowflake identifier, always quoted: names from SHOW keep their exact case."""
    return '"' + name.replace('"', '""') + '"'


def _explain(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = {}
    message = str(body.get("message", "")).strip()
    if response.status_code == 403:
        return f"Snowflake refused this for your role (403). {message}".strip()
    if response.status_code == 404:
        return f"Not found in Snowflake (404). {message}".strip()
    if response.status_code == 422:
        # A SQL error: Snowflake's own sentence is the useful part.
        return f"Snowflake: {message or 'the statement failed'}"
    if response.status_code == 429:
        return "Snowflake is busy (too many requests). Try again in a moment."
    return f"Snowflake answered HTTP {response.status_code}. {message}".strip()


class SnowflakeApi:
    """Snowflake's SQL API (/api/v2/statements) as one person, in one role."""

    def __init__(self, base_url: str, access_token: str, http: httpx.Client, *,
                 role: str = "") -> None:
        self._base = base_url.rstrip("/")
        self._role = role
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            # Both Snowflake OAuth and External OAuth tokens are OAUTH to the SQL API.
            "X-Snowflake-Authorization-Token-Type": "OAUTH",
            "Accept": "application/json",
            "User-Agent": "iq-connect/1.0",
        }
        self._http = http

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, f"{self._base}{path}",
                                          headers=self._headers, **kwargs)
        except httpx.HTTPError as error:
            raise AdapterError(f"Could not reach Snowflake: {type(error).__name__}") from None
        if response.status_code == 401:
            raise CredentialRejected("Snowflake rejected the sign-in (401).")
        if response.status_code not in (200, 202):
            raise AdapterError(_explain(response))
        return response

    def submit(self, statement: str, *, database: str = "", schema: str = "",
               warehouse: str = "", timeout: int = MAX_WAIT_SECONDS) -> dict[str, Any]:
        """Run one statement; the answer, or (HTTP 202) a body with its handle."""
        body: dict[str, Any] = {
            "statement": statement,
            "timeout": max(5, timeout),
            # Exactly one statement per request: never a second one smuggled in.
            "parameters": {"MULTI_STATEMENT_COUNT": "1", "QUERY_TAG": "iq-connect"},
        }
        for key, value in (("database", database), ("schema", schema),
                           ("warehouse", warehouse), ("role", self._role)):
            if value:
                body[key] = value
        response = self._send("POST", "/api/v2/statements",
                              params={"requestId": str(uuid.uuid4())}, json=body)
        found = response.json()
        found["_running"] = response.status_code == 202
        return found

    def status(self, handle: str) -> dict[str, Any]:
        response = self._send("GET", f"/api/v2/statements/{quote(handle, safe='')}")
        found = response.json()
        found["_running"] = response.status_code == 202
        return found

    def cancel(self, handle: str) -> None:
        self._send("POST", f"/api/v2/statements/{quote(handle, safe='')}/cancel")

    def wait(self, statement: str, *, wait_seconds: int = MAX_WAIT_SECONDS,
             **where: str) -> dict[str, Any]:
        """Run and wait up to `wait_seconds` for the answer (still running: `_running`)."""
        found = self.submit(statement, timeout=wait_seconds, **where)
        deadline = time.monotonic() + wait_seconds
        while found.get("_running") and found.get("statementHandle") \
                and time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
            found = self.status(found["statementHandle"])
        return found

    def rows(self, statement: str, **where: str) -> list[dict[str, Any]]:
        """A metadata statement's rows as dicts keyed by lower-case column name."""
        found = self.wait(statement, **where)
        if found.get("_running"):
            raise AdapterError("Snowflake took too long to answer. Try again.")
        names = [c.get("name", "").lower() for c in
                 (found.get("resultSetMetaData") or {}).get("rowType") or []]
        return [dict(zip(names, row, strict=False)) for row in found.get("data") or []]

    # --- browsing (no warehouse needed) ------------------------------------------------

    def warehouses(self) -> list[dict[str, str]]:
        found = self.rows("SHOW WAREHOUSES")
        return sorted(({"name": w["name"], "state": str(w.get("state") or ""),
                        "size": str(w.get("size") or "")} for w in found if w.get("name")),
                      key=lambda w: w["name"].lower())

    def databases(self) -> list[str]:
        return sorted((d["name"] for d in self.rows("SHOW DATABASES")
                       if d.get("name") and d["name"].lower() not in _SYSTEM_DATABASES),
                      key=str.lower)

    def schemas(self, database: str) -> list[str]:
        found = self.rows(f"SHOW SCHEMAS IN DATABASE {quote_ident(database)}")
        return sorted((s["name"] for s in found
                       if s.get("name") and s["name"].upper() != "INFORMATION_SCHEMA"),
                      key=str.lower)

    def objects(self, database: str, schema: str) -> list[dict[str, Any]]:
        return self.rows(f"SHOW TERSE OBJECTS IN SCHEMA "
                         f"{quote_ident(database)}.{quote_ident(schema)}")

    def columns(self, database: str, schema: str, table: str | None = None
                ) -> list[dict[str, Any]]:
        where = (f"TABLE {quote_ident(database)}.{quote_ident(schema)}.{quote_ident(table)}"
                 if table else f"SCHEMA {quote_ident(database)}.{quote_ident(schema)}")
        return self.rows(f"SHOW COLUMNS IN {where}")

    def keys(self, kind: str, database: str, schema: str) -> list[dict[str, Any]]:
        """`PRIMARY` or `IMPORTED` keys of a schema (declared, not enforced, in Snowflake)."""
        return self.rows(f"SHOW {kind} KEYS IN SCHEMA "
                         f"{quote_ident(database)}.{quote_ident(schema)}")

    def identity(self) -> dict[str, str]:
        """Who and which roles this token really runs as (no warehouse needed)."""
        found = self.rows("SELECT CURRENT_USER() AS USER_NAME, CURRENT_ROLE() AS ROLE_NAME, "
                          "CURRENT_SECONDARY_ROLES() AS SECONDARY_ROLES")
        return {k: str(v or "") for k, v in (found[0] if found else {}).items()}


def secondary_roles_active(value: str) -> bool:
    """CURRENT_SECONDARY_ROLES() is JSON like {"roles":"","value":""} when none are on."""
    try:
        parsed = json.loads(value or "{}")
    except ValueError:
        return bool(value.strip())
    return bool(str(parsed.get("roles") or parsed.get("value") or "").strip())

