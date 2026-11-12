"""
The Databricks REST APIs we use, called as the person (their access token):

  Unity Catalog  catalogs, schemas, tables and columns -- for browsing; needs no
                 running SQL warehouse, and only shows what the person may see
  SQL warehouses which warehouses the person can run queries on
  Statements     run one SQL statement on a warehouse and wait for the result;
                 one still running can be fetched later by its id, or cancelled
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from backend.core.dynamic_registry.base import AdapterError, CredentialRejected

# The Statement Execution API waits at most 50 s for a result within the request.
MAX_WAIT_SECONDS = 50
_MAX_PAGES = 20


def _explain(response: httpx.Response) -> str:
    try:
        message = response.json().get("message", "")
    except ValueError:
        message = ""
    if response.status_code == 403:
        return f"You don't have access to this in Databricks (403). {message}".strip()
    if response.status_code == 404:
        return f"Not found in Databricks (404). {message}".strip()
    return f"Databricks answered HTTP {response.status_code}. {message}".strip()


class DatabricksApi:
    def __init__(self, host: str, access_token: str, http: httpx.Client) -> None:
        self._host = host.rstrip("/")
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._http = http

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(method, f"{self._host}{path}",
                                          headers=self._headers, **kwargs)
        except httpx.HTTPError as error:
            raise AdapterError(f"Could not reach Databricks: {type(error).__name__}") from None
        if response.status_code == 401:
            raise CredentialRejected("Databricks rejected the sign-in (401).")
        if response.status_code != 200:
            raise AdapterError(_explain(response))
        return response.json()

    def _paged(self, path: str, key: str, params: dict[str, str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        token = ""
        for _ in range(_MAX_PAGES):
            page = self._request("GET", path, params={**params,
                                                      **({"page_token": token} if token else {})})
            items.extend(page.get(key) or [])
            token = page.get("next_page_token") or ""
            if not token:
                break
        return items

    # --- browsing ---------------------------------------------------------------

    def warehouses(self) -> list[dict[str, str]]:
        found = self._request("GET", "/api/2.0/sql/warehouses").get("warehouses") or []
        return sorted(({"id": w["id"], "name": w.get("name", ""), "state": w.get("state", "")}
                       for w in found if w.get("id")), key=lambda w: w["name"].lower())

    def catalogs(self) -> list[str]:
        found = self._paged("/api/2.1/unity-catalog/catalogs", "catalogs", {})
        return sorted(c["name"] for c in found if c.get("name"))

    def schemas(self, catalog: str) -> list[str]:
        found = self._paged("/api/2.1/unity-catalog/schemas", "schemas",
                            {"catalog_name": catalog})
        return sorted(s["name"] for s in found
                      if s.get("name") and s["name"].lower() != "information_schema")

    def tables(self, catalog: str, schema: str, *, with_columns: bool = False
               ) -> list[dict[str, Any]]:
        return self._paged("/api/2.1/unity-catalog/tables", "tables",
                           {"catalog_name": catalog, "schema_name": schema,
                            "omit_columns": "false" if with_columns else "true"})

    def table(self, full_name: str) -> dict[str, Any]:
        return self._request("GET", f"/api/2.1/unity-catalog/tables/{quote(full_name, safe='')}")

    # --- queries ----------------------------------------------------------------

    def execute(self, *, statement: str, warehouse_id: str, catalog: str, schema: str | None,
                row_limit: int, wait_seconds: int, keep_running: bool = False
                ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "statement": statement,
            "warehouse_id": warehouse_id,
            "catalog": catalog,
            "wait_timeout": f"{max(5, min(wait_seconds, MAX_WAIT_SECONDS))}s",
            # Not finished in time: Databricks cancels it, unless the caller will
            # come back for it by its statement id.
            "on_wait_timeout": "CONTINUE" if keep_running else "CANCEL",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
            "row_limit": row_limit,
        }
        if schema:
            body["schema"] = schema
        return self._request("POST", "/api/2.0/sql/statements", json=body)

    def statement(self, statement_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/2.0/sql/statements/{quote(statement_id, safe='')}")

    def cancel(self, statement_id: str) -> None:
        self._request("POST",
                      f"/api/2.0/sql/statements/{quote(statement_id, safe='')}/cancel")
