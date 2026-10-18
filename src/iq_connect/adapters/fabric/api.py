"""
The Fabric REST API, as the person: which workspaces and warehouses they can
see. Used when setting up a connection, never for answering queries.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..base import AdapterError

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
