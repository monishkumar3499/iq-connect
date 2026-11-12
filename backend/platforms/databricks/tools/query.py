"""
Databricks, queried as the person (their own Databricks OAuth token), so Unity
Catalog grants, row filters and column masks apply exactly as they do to them.

Connection shape:
  settings: {"warehouse_id", "host"}              the SQL warehouse queries run on, and the
                                                  workspace (each person brings their own;
                                                  no host = the server's DATABRICKS_HOST)
  scope:    {"catalog": <UC catalog>, "schemas": [...]}

Browsing uses the Unity Catalog API (no warehouse needed); queries use the SQL
Statement Execution API on the chosen warehouse, with the connection's catalog
and default schema set so unqualified names resolve exactly as the SQL policy
assumed when it checked them. A query Claude sends that is still running after
the wait keeps running and is fetched later by its statement id (start_query,
fetch_result); the map and profiling queries are cancelled instead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from backend.core.dynamic_registry.base import (
    AdapterError,
    CatalogMeta,
    Column,
    ColumnMeta,
    ConnectionContext,
    DbObject,
    Rows,
    TableInfo,
)
from backend.platforms.databricks.auth import AZURE_DATABRICKS_SCOPE, TOKEN_SCOPE
from backend.platforms.databricks.client import MAX_WAIT_SECONDS, DatabricksApi


class DatabricksAdapter:
    platform = "databricks"
    label = "Databricks"
    dialect = "databricks"
    capabilities = frozenset({"sql", "query_ids"})
    token_scopes = (TOKEN_SCOPE,)
    # Which resource to ask each sign-in provider for.
    token_scopes_by_provider = {"databricks": (TOKEN_SCOPE,),
                                "entra": (AZURE_DATABRICKS_SCOPE,)}

    def token_scopes_for(self, conn: ConnectionContext, provider: str) -> tuple[str, ...]:
        return self.token_scopes_by_provider.get(provider, self.token_scopes)

    def __init__(self, *, host: str | Callable[[], str], query_timeout: int,
                 http: Callable[[], httpx.Client] | None = None) -> None:
        # A string, or a function: the workspace may be entered in the setup UI.
        self._host = host
        self._query_timeout = query_timeout
        # One HTTP client, reused across calls; tests pass a fake transport.
        self._http = (http or (lambda: httpx.Client(timeout=MAX_WAIT_SECONDS + 15)))()

    @property
    def host(self) -> str:
        host = self._host() if callable(self._host) else self._host
        return host.rstrip("/")

    def api(self, secret: dict[str, Any] | None, host: str = "") -> DatabricksApi:
        tokens = (secret or {}).get("access_tokens") or {}
        token = tokens.get(TOKEN_SCOPE) or tokens.get(AZURE_DATABRICKS_SCOPE)
        if not token:
            raise AdapterError("No Databricks sign-in is linked to this connection.")
        host = (host or self.host).rstrip("/")
        if not host:
            raise AdapterError("This connection has no Databricks workspace saved. Connect it "
                               "again from Add a new connection.")
        return DatabricksApi(host, token, self._http)

    def _api(self, conn: ConnectionContext, secret: dict[str, Any] | None) -> DatabricksApi:
        return self.api(secret, str(conn.settings.get("host") or ""))

    def close(self) -> None:
        self._http.close()

    # --- the adapter interface ----------------------------------------------------

    def list_objects(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                     schema: str | None) -> list[DbObject]:
        api = self._api(conn, secret)
        objects = []
        for name in ([schema] if schema else conn.schemas):
            for table in api.tables(conn.catalog, name):
                kind = "view" if "VIEW" in str(table.get("table_type", "")).upper() else "table"
                objects.append(DbObject(table.get("schema_name", name), table["name"], kind))
        return sorted(objects, key=lambda o: (o.schema.lower(), o.name.lower()))

    def describe_table(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                       schema: str, table: str) -> TableInfo:
        info = self._api(conn, secret).table(f"{conn.catalog}.{schema}.{table}")
        columns = [
            Column(name=c.get("name", ""), type=str(c.get("type_text") or c.get("type_name", "")),
                   nullable=bool(c.get("nullable", True)), comment=c.get("comment") or "")
            for c in sorted(info.get("columns") or [], key=lambda c: c.get("position", 0))
        ]
        kind = "view" if "VIEW" in str(info.get("table_type", "")).upper() else "table"
        return TableInfo(schema, info.get("name", table), kind, columns,
                         comment=info.get("comment") or "")

    def run_query(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                  sql: str, limit: int) -> Rows:
        return self._execute(conn, secret, sql, limit, keep_running=False)

    def start_query(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                    sql: str, limit: int) -> Rows:
        return self._execute(conn, secret, sql, limit, keep_running=True)

    def fetch_result(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                     query_id: str, limit: int) -> Rows:
        return _rows(self._api(conn, secret).statement(query_id), limit, keep_running=True)

    def cancel_query(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                     query_id: str) -> None:
        self._api(conn, secret).cancel(query_id)

    def _execute(self, conn: ConnectionContext, secret: dict[str, Any] | None, sql: str,
                 limit: int, *, keep_running: bool) -> Rows:
        warehouse_id = str(conn.settings.get("warehouse_id", ""))
        if not warehouse_id:
            raise AdapterError(f"The connection '{conn.name}' has no SQL warehouse chosen.")
        result = self._api(conn, secret).execute(
            statement=sql, warehouse_id=warehouse_id, catalog=conn.catalog,
            schema=conn.default_schema, row_limit=limit + 1,
            wait_seconds=self._query_timeout, keep_running=keep_running,
        )
        return _rows(result, limit, keep_running=keep_running)

    def catalog(self, conn: ConnectionContext, secret: dict[str, Any] | None) -> CatalogMeta:
        """
        Columns and declared constraints from Unity Catalog, one list call per
        schema. Databricks constraints are informational (not enforced), which
        is exactly how the map treats declared keys anyway.
        """
        api = self._api(conn, secret)
        meta = CatalogMeta()
        for schema in conn.schemas:
            for table in api.tables(conn.catalog, schema, with_columns=True):
                name = table["name"]
                kind = "VIEW" if "VIEW" in str(table.get("table_type", "")).upper() else "TABLE"
                for column in table.get("columns") or []:
                    meta.columns.append(ColumnMeta(
                        schema, name, kind, column.get("name", ""),
                        str(column.get("type_text") or column.get("type_name", "")),
                        bool(column.get("nullable", True)), int(column.get("position", 0)) + 1))
                for constraint in table.get("table_constraints") or []:
                    primary = constraint.get("primary_key_constraint")
                    if primary:
                        meta.primary_keys += [(schema, name, c)
                                              for c in primary.get("child_columns", [])]
                    foreign = constraint.get("foreign_key_constraint")
                    if foreign:
                        parent = str(foreign.get("parent_table", "")).split(".")
                        # Only edges inside this connection's catalog are useful here.
                        if len(parent) == 3 and parent[0].lower() == conn.catalog.lower():
                            meta.foreign_keys += [
                                (schema, name, child, parent[1], parent[2], target)
                                # Not strict: a malformed constraint from the API
                                # should lose an edge, not the whole map.
                                for child, target in zip(foreign.get("child_columns", []),
                                                         foreign.get("parent_columns", []),
                                                         strict=False)
                            ]
        return meta


def _rows(result: dict[str, Any], limit: int, *, keep_running: bool) -> Rows:
    """A statement's answer as Rows: its rows, still running, or why it failed."""
    status = result.get("status") or {}
    state = status.get("state")
    if state in {"PENDING", "RUNNING"} and keep_running and result.get("statement_id"):
        return Rows([], [], still_running=str(result["statement_id"]))
    if state != "SUCCEEDED":
        message = (status.get("error") or {}).get("message", "")
        # Without keep_running, Databricks cancels at the wait timeout itself.
        if state in {"PENDING", "RUNNING"} or (state == "CANCELED" and not keep_running):
            raise AdapterError("The query did not finish in time and was cancelled. "
                               "Aggregate more, or filter to fewer rows.")
        if state == "CANCELED":
            raise AdapterError("The query was cancelled before it finished.")
        if state == "CLOSED":
            raise AdapterError("Databricks no longer keeps this result. Run the query again.")
        raise AdapterError(f"Databricks: {message or state or 'the query failed'}")
    columns = [c.get("name", "") for c in
               ((result.get("manifest") or {}).get("schema") or {}).get("columns") or []]
    data = (result.get("result") or {}).get("data_array") or []
    return Rows(columns, [tuple(row) for row in data[:limit]], truncated=len(data) > limit)
