"""Snowflake's implementation of the common tools (see client.py for the SQL API)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
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
from backend.platforms.snowflake.auth import SNOWFLAKE_SCOPE, account_url
from backend.platforms.snowflake.client import MAX_WAIT_SECONDS, SnowflakeApi

# --- turning the SQL API's text values into Python values ----------------------------------

_EPOCH = date(1970, 1, 1)


def _convert(value: Any, column: dict[str, Any]) -> Any:
    """The SQL API (jsonv2) sends every value as text; give each its real type."""
    if value is None:
        return None
    kind = str(column.get("type", "")).lower()
    try:
        if kind == "fixed":
            return int(value) if int(column.get("scale") or 0) == 0 else Decimal(value)
        if kind == "real":
            return float(value)
        if kind == "boolean":
            return str(value).lower() in ("true", "1")
        if kind == "date":
            return _EPOCH + timedelta(days=int(value))
        if kind in ("timestamp_ntz", "timestamp_ltz"):
            moment = datetime.fromtimestamp(float(value), tz=UTC)
            return moment.replace(tzinfo=None) if kind == "timestamp_ntz" else moment
        if kind == "timestamp_tz":
            # "<UTC epoch seconds> <offset>", the offset in minutes plus 1440.
            seconds, _, offset = str(value).partition(" ")
            zone = timezone(timedelta(minutes=int(offset) - 1440)) if offset else UTC
            return datetime.fromtimestamp(float(seconds), tz=zone)
    except (ValueError, ArithmeticError, OverflowError):
        return value
    return value


def _to_rows(found: dict[str, Any], limit: int, *, keep_running: bool) -> Rows:
    if found.get("_running"):
        handle = str(found.get("statementHandle") or "")
        if keep_running and handle:
            return Rows([], [], still_running=handle)
        raise AdapterError("The query did not finish in time. Aggregate more, or filter "
                           "to fewer rows.")
    meta = found.get("resultSetMetaData") or {}
    columns = meta.get("rowType") or []
    data = found.get("data") or []
    rows = [tuple(_convert(v, c) for v, c in zip(row, columns, strict=False))
            for row in data[:limit]]
    total = int(meta.get("numRows") or len(data))
    return Rows([c.get("name", "") for c in columns], rows, truncated=total > limit)


# --- the adapter -------------------------------------------------------------------------------


class SnowflakeAdapter:
    platform = "snowflake"
    label = "Snowflake"
    dialect = "snowflake"
    capabilities = frozenset({"sql", "query_ids"})

    def __init__(self, *, base_url: str | Callable[[], str], role: str = "",
                 entra_scope: str = "", query_timeout: int = MAX_WAIT_SECONDS,
                 http: Callable[[], httpx.Client] | None = None) -> None:
        # The server's defaults; each connection names its own account, role and scope.
        self._base_url = base_url
        self._role = role
        self._entra_scope = entra_scope
        self._query_timeout = query_timeout
        self.token_scopes = (SNOWFLAKE_SCOPE,)
        # One HTTP client, reused across calls; tests pass a fake transport.
        self._http = (http or (lambda: httpx.Client(timeout=MAX_WAIT_SECONDS + 15)))()

    @property
    def base_url(self) -> str:
        return self._base_url() if callable(self._base_url) else self._base_url

    def token_scopes_for(self, conn: ConnectionContext, provider: str) -> tuple[str, ...]:
        """Snowflake OAuth needs no scope; External OAuth asks Entra for the role's scope."""
        if provider == "entra":
            scope = str(conn.settings.get("entra_scope") or self._entra_scope)
            if not scope:
                raise AdapterError(f"The connection '{conn.name}' has no Snowflake External "
                                   "OAuth scope saved. Connect it again.")
            return (scope,)
        return self.token_scopes

    def api(self, secret: dict[str, Any] | None, role: str | None = None,
            account: str = "") -> SnowflakeApi:
        tokens = (secret or {}).get("access_tokens") or {}
        token = next(iter(tokens.values()), "")
        if not token:
            raise AdapterError("No Snowflake sign-in is linked to this connection.")
        base = account_url(account) if account else self.base_url
        if not base:
            raise AdapterError("This connection has no Snowflake account saved.")
        return SnowflakeApi(base, token, self._http,
                            role=self._role if role is None else role)

    def close(self) -> None:
        self._http.close()

    def _api_for(self, conn: ConnectionContext, secret: dict[str, Any] | None) -> SnowflakeApi:
        # The role the connection was validated with, never anything wider.
        return self.api(secret, str(conn.settings.get("role") or self._role),
                        str(conn.settings.get("account") or ""))

    # --- the adapter interface ----------------------------------------------------------

    def list_objects(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                     schema: str | None) -> list[DbObject]:
        api = self._api_for(conn, secret)
        objects = []
        for name in ([schema] if schema else conn.schemas):
            for row in api.objects(conn.catalog, name):
                kind = "view" if "VIEW" in str(row.get("kind", "")).upper() else "table"
                objects.append(DbObject(str(row.get("schema_name") or name), row["name"], kind))
        return sorted(objects, key=lambda o: (o.schema.lower(), o.name.lower()))

    def describe_table(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                       schema: str, table: str) -> TableInfo:
        api = self._api_for(conn, secret)
        found = api.columns(conn.catalog, schema, table)
        if not found:
            raise AdapterError(f"Table {schema}.{table} was not found, or you can't read it.")
        kinds = {o["name"]: o.get("kind", "") for o in api.objects(conn.catalog, schema)}
        kind = "view" if "VIEW" in str(kinds.get(table, "")).upper() else "table"
        return TableInfo(schema, table, kind, [_column(c) for c in found])

    def run_query(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                  sql: str, limit: int) -> Rows:
        return self._execute(conn, secret, sql, limit, keep_running=False)

    def start_query(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                    sql: str, limit: int) -> Rows:
        return self._execute(conn, secret, sql, limit, keep_running=True)

    def fetch_result(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                     query_id: str, limit: int) -> Rows:
        return _to_rows(self._api_for(conn, secret).status(query_id), limit, keep_running=True)

    def cancel_query(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                     query_id: str) -> None:
        self._api_for(conn, secret).cancel(query_id)

    def _execute(self, conn: ConnectionContext, secret: dict[str, Any] | None, sql: str,
                 limit: int, *, keep_running: bool) -> Rows:
        warehouse = str(conn.settings.get("warehouse", ""))
        if not warehouse:
            raise AdapterError(f"The connection '{conn.name}' has no warehouse chosen.")
        api = self._api_for(conn, secret)
        found = api.wait(sql, wait_seconds=min(self._query_timeout, MAX_WAIT_SECONDS),
                         database=conn.catalog, schema=conn.default_schema or "",
                         warehouse=warehouse)
        if found.get("_running") and not keep_running and found.get("statementHandle"):
            api.cancel(found["statementHandle"])
        return _to_rows(found, limit, keep_running=keep_running)

    def catalog(self, conn: ConnectionContext, secret: dict[str, Any] | None) -> CatalogMeta:
        """Columns and declared keys of the connection's schemas, from SHOW commands."""
        api = self._api_for(conn, secret)
        meta = CatalogMeta()
        for schema in conn.schemas:
            kinds = {o["name"]: ("VIEW" if "VIEW" in str(o.get("kind", "")).upper()
                                 else "TABLE") for o in api.objects(conn.catalog, schema)}
            ordinals: dict[str, int] = {}
            for row in api.columns(conn.catalog, schema):
                table = row.get("table_name", "")
                ordinals[table] = ordinals.get(table, 0) + 1
                column = _column(row)
                meta.columns.append(ColumnMeta(schema, table, kinds.get(table, "TABLE"),
                                               column.name, column.type, column.nullable,
                                               ordinals[table]))
            for key in api.keys("PRIMARY", conn.catalog, schema):
                meta.primary_keys.append((schema, key.get("table_name", ""),
                                          key.get("column_name", "")))
            for key in api.keys("IMPORTED", conn.catalog, schema):
                # Only edges inside this connection's database are useful here.
                if str(key.get("pk_database_name", "")).lower() != conn.catalog.lower():
                    continue
                meta.foreign_keys.append((
                    schema, key.get("fk_table_name", ""), key.get("fk_column_name", ""),
                    key.get("pk_schema_name", ""), key.get("pk_table_name", ""),
                    key.get("pk_column_name", "")))
        return meta


def _column(row: dict[str, Any]) -> Column:
    """One SHOW COLUMNS row: data_type is JSON like {"type":"FIXED","precision":38,...}."""
    raw = row.get("data_type") or "{}"
    try:
        spec = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except ValueError:
        spec = {"type": str(raw)}
    kind = str(spec.get("type", "")).upper()
    if kind == "FIXED":
        text = f"NUMBER({spec.get('precision', 38)},{spec.get('scale', 0)})"
    elif kind == "TEXT" and spec.get("length"):
        text = f"VARCHAR({spec['length']})"
    else:
        text = kind or "UNKNOWN"
    nullable = spec.get("nullable")
    if nullable is None:
        nullable = str(row.get("null?", "true")).lower() in ("true", "y", "yes")
    return Column(name=str(row.get("column_name", "")), type=text, nullable=bool(nullable),
                  comment=str(row.get("comment") or ""))
