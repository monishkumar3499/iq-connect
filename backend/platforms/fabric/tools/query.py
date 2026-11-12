"""
Microsoft Fabric Warehouse, queried as the person (their own Entra token).

Every query runs under the person's Fabric permissions: the access token the
pipeline hands over was minted from *their* linked Microsoft account, so the
warehouse's own security (object grants, row-level security, masking) applies
exactly as it would to them in Fabric.

Connection shape:
  settings: {"workspace_id", "warehouse_id", "sql_endpoint"}
  scope:    {"catalog": <warehouse database name>, "schemas": [...]}

Connections are reused between queries through `FabricConnectionPool` (see
pool.py for the rules that keep that safe); without a pool, every query opens
and closes its own.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import closing
from typing import Any

import httpx

from backend.core.dynamic_registry.base import (
    AdapterError,
    CatalogMeta,
    Column,
    ColumnMeta,
    ConnectionContext,
    CredentialRejected,
    DbObject,
    Rows,
    TableInfo,
)
from backend.platforms.fabric.auth import POWERBI_SCOPE, SQL_SCOPE
from backend.platforms.fabric.client import (
    FabricConnectionPool,
    Key,
    execute_dax,
    token_fingerprint,
)

log = logging.getLogger(__name__)

# The ODBC attribute that passes an Entra access token to the SQL Server driver.
SQL_COPT_SS_ACCESS_TOKEN = 1256
ODBC_DRIVER = "ODBC Driver 18 for SQL Server"


def token_struct(access_token: str) -> bytes:
    """The driver wants the token as UTF-16-LE bytes behind a 4-byte length."""
    raw = access_token.encode("utf-16-le")
    return len(raw).to_bytes(4, "little") + raw


def _default_connect(connection_string: str, **kwargs: Any) -> Any:
    try:
        import pyodbc
    except ImportError as error:  # pragma: no cover - deployment problem
        raise AdapterError(
            "pyodbc is not installed, or Microsoft ODBC Driver 18 for SQL Server is missing."
        ) from error
    return pyodbc.connect(connection_string, **kwargs)


def _message(error: BaseException) -> str:
    args = getattr(error, "args", ())
    text = str(args[-1]) if args else str(error)
    return text.strip().splitlines()[0][:500] if text.strip() else type(error).__name__


class FabricAdapter:
    platform = "fabric"
    label = "Microsoft Fabric Warehouse"
    dialect = "tsql"
    capabilities = frozenset({"sql", "semantic_model"})
    # What the pipeline must mint from the person's linked Microsoft account.
    token_scopes = (SQL_SCOPE,)

    def __init__(self, *, login_timeout: int, query_timeout: int,
                 connect: Callable[..., Any] | None = None,
                 pool: FabricConnectionPool | None = None,
                 http: Callable[[], httpx.Client] | None = None) -> None:
        self._login_timeout = login_timeout
        self._query_timeout = query_timeout
        self._connect = connect or _default_connect  # tests pass a fake
        self._pool = pool  # None: a fresh connection for every query
        # For the Power BI API (semantic models); tests pass a fake transport.
        self._http_factory = http or (lambda: httpx.Client(timeout=120.0))
        self._http: httpx.Client | None = None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close_all()
        if self._http is not None:
            self._http.close()

    def _target(self, conn: ConnectionContext, secret: dict[str, Any] | None
                ) -> tuple[str, str, Key]:
        endpoint = str(conn.settings.get("sql_endpoint", "")).strip()
        token = ((secret or {}).get("access_tokens") or {}).get(SQL_SCOPE)
        if not endpoint and self.allowed_models(conn):
            raise AdapterError(
                f"'{conn.name}' is a semantic-model connection: use semantic_model_map and "
                "query_semantic_model with it, not SQL."
            )
        if not endpoint or not conn.catalog:
            raise AdapterError(
                f"The connection '{conn.name}' has no warehouse address saved. Recreate it."
            )
        if not token:
            raise AdapterError("No Microsoft sign-in is linked to this connection.")
        # Braces stop a value from injecting extra connection-string settings.
        database = conn.catalog.replace("}", "}}")
        connection_string = (
            f"DRIVER={{{ODBC_DRIVER}}};SERVER={endpoint};DATABASE={{{database}}};"
            "Encrypt=yes;TrustServerCertificate=no;ApplicationIntent=ReadOnly;"
        )
        key = (conn.id, endpoint.lower(), conn.catalog, token_fingerprint(token))
        return connection_string, token, key

    def _open_new(self, connection_string: str, token: str) -> Any:
        try:
            session = self._connect(
                connection_string,
                attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct(token)},
                timeout=self._login_timeout,
            )
        except AdapterError:
            raise
        except Exception as error:
            raise AdapterError(f"Fabric refused the connection: {_message(error)}") from None
        session.autocommit = True
        # pyodbc applies the query timeout per connection, not per cursor.
        session.timeout = self._query_timeout
        return session

    def _fetch(self, conn: ConnectionContext, secret: dict[str, Any] | None, sql: str,
               params: tuple[Any, ...] = (), limit: int | None = None) -> Rows:
        connection_string, token, key = self._target(conn, secret)
        reused = self._pool.take(key) if self._pool is not None else None
        if reused is not None:
            session, opened = reused
            try:
                return self._run_and_return(key, session, opened, sql, params, limit)
            except _ConnectionLost:
                # The pooled connection had died while idle (network blip,
                # server-side timeout). Only read-only SELECTs ever run here,
                # so running it again on a fresh connection is safe.
                log.info("Pooled Fabric connection was dead; retrying on a new one")
        session = self._open_new(connection_string, token)
        try:
            opened = self._pool.now() if self._pool is not None else time.monotonic()
            return self._run_and_return(key, session, opened, sql, params, limit)
        except _ConnectionLost as lost:
            raise AdapterError(f"Fabric: {lost.text}") from None

    def _run_and_return(self, key: Key, session: Any, opened: float, sql: str,
                        params: tuple[Any, ...], limit: int | None) -> Rows:
        try:
            with closing(session.cursor()) as cursor:
                cursor.execute(sql, *params)
                rows = _collect(cursor, limit)
        except Exception as error:
            _close_quietly(session)  # its state is unknown: never reuse it
            if _is_connection_error(error):
                raise _ConnectionLost(_message(error)) from None
            raise AdapterError(f"Fabric: {_message(error)}") from None
        if self._pool is not None:
            self._pool.give_back(key, session, opened)
        else:
            _close_quietly(session)
        return rows

    # --- the adapter interface ----------------------------------------------------

    def list_objects(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                     schema: str | None) -> list[DbObject]:
        schemas = [schema] if schema else conn.schemas
        if not schemas:
            return []
        marks = ", ".join("?" for _ in schemas)
        rows = self._fetch(conn, secret, (
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_SCHEMA IN ({marks}) ORDER BY TABLE_SCHEMA, TABLE_NAME"
        ), tuple(schemas))
        return [DbObject(r[0], r[1], "view" if r[2] == "VIEW" else "table") for r in rows.rows]

    def describe_table(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                       schema: str, table: str) -> TableInfo:
        rows = self._fetch(conn, secret, (
            "SELECT c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE, c.CHARACTER_MAXIMUM_LENGTH, "
            "c.NUMERIC_PRECISION, c.NUMERIC_SCALE, t.TABLE_TYPE "
            "FROM INFORMATION_SCHEMA.COLUMNS c JOIN INFORMATION_SCHEMA.TABLES t "
            "ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
            "WHERE c.TABLE_SCHEMA = ? AND c.TABLE_NAME = ? ORDER BY c.ORDINAL_POSITION"
        ), (schema, table))
        if not rows.rows:
            raise AdapterError(f"No table or view {schema}.{table} in {conn.catalog}.")
        columns = [
            Column(name=r[0], type=_type_text(r[1], r[3], r[4], r[5]), nullable=r[2] == "YES")
            for r in rows.rows
        ]
        kind = "view" if rows.rows[0][6] == "VIEW" else "table"
        return TableInfo(schema, table, kind, columns)

    def run_query(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                  sql: str, limit: int) -> Rows:
        return self._fetch(conn, secret, sql, limit=limit)

    def catalog(self, conn: ConnectionContext, secret: dict[str, Any] | None) -> CatalogMeta:
        """
        The prototype's three INFORMATION_SCHEMA queries, limited to the
        connection's schemas. Constraint metadata is optional: Fabric does not
        enforce keys and many warehouses declare none -- the map then infers.
        """
        meta = CatalogMeta()
        if not conn.schemas:
            return meta
        marks = ", ".join("?" for _ in conn.schemas)
        schemas = tuple(conn.schemas)
        rows = self._fetch(conn, secret, (
            "SELECT c.TABLE_SCHEMA, c.TABLE_NAME, t.TABLE_TYPE, c.COLUMN_NAME, c.DATA_TYPE, "
            "c.IS_NULLABLE, c.ORDINAL_POSITION, c.CHARACTER_MAXIMUM_LENGTH, "
            "c.NUMERIC_PRECISION, c.NUMERIC_SCALE "
            "FROM INFORMATION_SCHEMA.COLUMNS c JOIN INFORMATION_SCHEMA.TABLES t "
            "ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
            f"WHERE c.TABLE_SCHEMA IN ({marks}) "
            "ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION"
        ), schemas)
        for r in rows.rows:
            meta.columns.append(ColumnMeta(
                r[0], r[1], "VIEW" if r[2] == "VIEW" else "TABLE", r[3],
                _type_text(r[4], r[7], r[8], r[9]), r[5] == "YES", int(r[6] or 0)))
        try:
            keys = self._fetch(conn, secret, (
                "SELECT k.TABLE_SCHEMA, k.TABLE_NAME, k.COLUMN_NAME "
                "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k "
                "ON k.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
                "AND k.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA "
                f"WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND k.TABLE_SCHEMA IN ({marks}) "
                "ORDER BY k.TABLE_SCHEMA, k.TABLE_NAME, k.ORDINAL_POSITION"
            ), schemas)
            meta.primary_keys = [(r[0], r[1], r[2]) for r in keys.rows]
            fks = self._fetch(conn, secret, (
                "SELECT fk.TABLE_SCHEMA, fk.TABLE_NAME, fk.COLUMN_NAME, "
                "pk.TABLE_SCHEMA, pk.TABLE_NAME, pk.COLUMN_NAME "
                "FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE fk "
                "ON fk.CONSTRAINT_NAME = rc.CONSTRAINT_NAME "
                "AND fk.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE pk "
                "ON pk.CONSTRAINT_NAME = rc.UNIQUE_CONSTRAINT_NAME "
                "AND pk.CONSTRAINT_SCHEMA = rc.UNIQUE_CONSTRAINT_SCHEMA "
                "AND pk.ORDINAL_POSITION = fk.ORDINAL_POSITION "
                f"WHERE fk.TABLE_SCHEMA IN ({marks})"
            ), schemas)
            meta.foreign_keys = [tuple(r[:6]) for r in fks.rows]
        except AdapterError:
            pass  # name-shape inference covers a warehouse without constraint metadata
        return meta

    # --- semantic models (capability "semantic_model") --------------------------------

    @staticmethod
    def allowed_models(conn: ConnectionContext) -> list[dict[str, str]]:
        """The semantic models this connection may use, chosen in the wizard."""
        return [m for m in conn.settings.get("semantic_models", []) if m.get("id")]

    def resolve_model(self, conn: ConnectionContext, model: str) -> dict[str, str]:
        """A model allowed by this connection, by name or id; the only one if blank."""
        allowed = self.allowed_models(conn)
        if not allowed:
            raise AdapterError(f"The connection '{conn.name}' has no semantic models. Add "
                               "them in the IQ settings app (New connection, Fabric).")
        wanted = model.strip().lower()
        if not wanted:
            if len(allowed) == 1:
                return allowed[0]
            raise AdapterError("Name the semantic model: " + ", ".join(m["name"] for m in allowed))
        for item in allowed:
            if wanted in (item["id"].lower(), item.get("name", "").lower()):
                return item
        raise AdapterError(f"'{model}' is not one of this connection's semantic models: "
                           + ", ".join(m["name"] for m in allowed))

    def _powerbi(self) -> httpx.Client:
        if self._http is None:
            self._http = self._http_factory()
        return self._http

    def execute_dax(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                    model_id: str, dax: str, limit: int) -> Rows:
        token = ((secret or {}).get("access_tokens") or {}).get(POWERBI_SCOPE)
        if not token:
            raise AdapterError("No Microsoft sign-in is linked to this connection.")
        workspace_id = str(conn.settings.get("workspace_id", ""))
        rows = execute_dax(self._powerbi(), token, workspace_id, model_id, dax)
        return Rows(rows.columns, rows.rows[:limit], truncated=len(rows.rows) > limit)

    def model_map(self, conn: ConnectionContext, secret: dict[str, Any] | None,
                  model_id: str, include_columns: bool) -> list[tuple[str, Rows | None]]:
        """
        Measures (with their DAX), tables and relationships, from the model's
        INFO functions. INFO.VIEW.* is richer but not on every model version,
        hence the fallbacks -- as in the prototype.
        """
        requests = [
            ("Measures",
             'EVALUATE SELECTCOLUMNS(INFO.VIEW.MEASURES(), "Table", [Table], "Measure", [Name], '
             '"Expression", [Expression], "Format", [FormatString], "Folder", [DisplayFolder])',
             'EVALUATE SELECTCOLUMNS(INFO.MEASURES(), "Measure", [Name], '
             '"Expression", [Expression])'),
            ("Tables",
             'EVALUATE SELECTCOLUMNS(INFO.VIEW.TABLES(), "Table", [Name], "Hidden", [IsHidden], '
             '"Description", [Description])',
             'EVALUATE SELECTCOLUMNS(INFO.TABLES(), "Table", [Name])'),
            ("Relationships",
             'EVALUATE SELECTCOLUMNS(INFO.VIEW.RELATIONSHIPS(), "Relationship", '
             '[Relationship], "Active", [IsActive], "CrossFilter", [CrossFilteringBehavior])',
             None),
        ]
        if include_columns:
            requests.append(("Columns", 'EVALUATE SELECTCOLUMNS(INFO.VIEW.COLUMNS(), "Table", '
                             '[Table], "Column", [Name], "DataType", [DataType], '
                             '"Hidden", [IsHidden])', None))
        sections: list[tuple[str, Rows | None]] = []
        for title, primary, fallback in requests:
            found: Rows | None = None
            for query in (primary, fallback):
                if not query:
                    continue
                try:
                    found = self.execute_dax(conn, secret, model_id, query, 5000)
                    break
                except CredentialRejected:
                    raise
                except AdapterError:
                    continue  # try the fallback
            sections.append((title, found))
        return sections

    # --- setup only ---------------------------------------------------------------

    def list_schemas(self, conn: ConnectionContext, secret: dict[str, Any] | None) -> list[str]:
        """The warehouse's own schemas, for choosing a connection's scope."""
        rows = self._fetch(conn, secret, (
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME"
        ))
        return [r[0] for r in rows.rows if str(r[0]).lower() not in _SYSTEM_SCHEMAS
                and not str(r[0]).lower().startswith("db_")]


# Built-in schemas that hold no business data.
_SYSTEM_SCHEMAS = {"sys", "information_schema", "guest", "queryinsights"}


class _ConnectionLost(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def _is_connection_error(error: BaseException) -> bool:
    # ODBC reports the SQLSTATE first; class 08 is "connection exception"
    # (e.g. 08S01 communication link failure).
    args = getattr(error, "args", ())
    return bool(args) and str(args[0]).startswith("08")


def _collect(cursor: Any, limit: int | None) -> Rows:
    if cursor.description is None:
        return Rows([], [])
    columns = [d[0] for d in cursor.description]
    if limit is None:
        return Rows(columns, [tuple(r) for r in cursor.fetchall()])
    fetched = cursor.fetchmany(limit + 1)
    return Rows(columns, [tuple(r) for r in fetched[:limit]], truncated=len(fetched) > limit)


def _close_quietly(session: Any) -> None:
    try:
        session.close()
    except Exception:  # noqa: BLE001 - closing a broken connection
        pass


def _type_text(data_type: str, length: Any, precision: Any, scale: Any) -> str:
    kind = str(data_type)
    if length not in (None, 0):
        return f"{kind}({'max' if length == -1 else length})"
    if kind.lower() in {"decimal", "numeric"} and precision is not None:
        return f"{kind}({precision},{scale or 0})"
    return kind
