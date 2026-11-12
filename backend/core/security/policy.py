"""The read-only policies: SQL (sqlglot allow-list per dialect, scope) and DAX."""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from backend.core.dynamic_registry.base import ConnectionContext

MAX_SQL_LENGTH = 100_000

# Node types that must not appear anywhere in an allowed query.
_FORBIDDEN = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
        "Command", "Into", "Lock", "Copy", "Set", "Use", "Transaction", "Commit", "Rollback",
        "Pragma", "Grant", "Revoke", "Execute", "LoadData", "Analyze", "Lateral",
    )
    if hasattr(exp, name)
)

_ALLOWED_ROOTS = (exp.Select, exp.SetOperation)

# Functions refused inside any query, whatever the dialect: a query is checked
# with every platform's list, so a name that means nothing on one platform is
# simply never used there. Names are compared case-insensitively; a schema
# prefix (sys.fn_dblog) doesn't help.
_DENIED_FUNCTIONS = frozenset({
    # Databricks / Spark: run Java, read secrets or files, call the network or a model.
    "reflect", "java_method", "try_reflect", "secret", "try_secret", "read_files",
    "read_kafka", "read_kinesis", "read_pubsub", "read_pulsar", "read_statestore",
    "read_state_metadata", "cloud_files_state", "http_request", "vector_search",
    # T-SQL (Fabric warehouses): other servers, files, the transaction log.
    "openquery", "openrowset", "opendatasource", "openxml", "fn_get_audit_file",
    "fn_xe_file_target_read_file", "fn_trace_gettable", "fn_dblog", "fn_dump_dblog",
    # Snowflake: files on stages.
    "get_presigned_url", "build_scoped_file_url", "build_stage_file_url",
    # SQLite (demo data): extensions and files.
    "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer",
})
_DENIED_PREFIXES = (
    "ai_",      # Databricks AI functions: send data to a model and bill for it
    "xp_",      # T-SQL extended procedures (xp_cmdshell, ...)
    "sp_",      # T-SQL system procedures
    "system$",  # Snowflake system functions
)


class QueryRejected(Exception):
    """The query is not allowed. The message says why, in words Claude can act on."""


@dataclass(frozen=True)
class TableRef:
    catalog: str
    schema: str
    name: str

    def __str__(self) -> str:
        return ".".join(part for part in (self.catalog, self.schema, self.name) if part)


def check_query(sql: str, dialect: str, conn: ConnectionContext) -> list[TableRef]:
    """Raise QueryRejected unless `sql` is a single in-scope read. Returns the tables it reads."""
    if not sql.strip():
        raise QueryRejected("The query is empty.")
    if len(sql) > MAX_SQL_LENGTH:
        raise QueryRejected("The query is too long.")
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError:
        raise QueryRejected(
            "The query could not be parsed as a single read-only statement. "
            f"Write standard {dialect} SQL without vendor extensions."
        ) from None
    if len(statements) != 1:
        raise QueryRejected("Send exactly one statement (no semicolon-separated batches).")

    statement = statements[0]
    if not isinstance(statement, _ALLOWED_ROOTS):
        raise QueryRejected("Only SELECT queries are allowed: this connection is read-only.")
    for node in statement.walk():
        if isinstance(node, _FORBIDDEN):
            raise QueryRejected(
                f"{node.key.upper()} is not allowed: this connection is read-only."
            )

    _check_sources(statement)
    _check_functions(statement)
    return _check_scope(statement, conn)


def _check_functions(statement: exp.Expression) -> None:
    for node in statement.find_all(exp.Func):
        name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).casefold()
        if name in _DENIED_FUNCTIONS or name.startswith(_DENIED_PREFIXES):
            raise QueryRejected(
                f"The function {name.upper()} is not allowed: it can reach beyond the "
                "connection's tables (code, files, secrets, the network or the server)."
            )


def _check_sources(statement: exp.Expression) -> None:
    for clause in [*statement.find_all(exp.From), *statement.find_all(exp.Join)]:
        source = clause.this
        if isinstance(source, exp.Subquery):
            continue
        if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
            continue
        raise QueryRejected(
            "Only tables, views, subqueries and CTEs may appear in FROM and JOIN "
            "(no table functions or file readers)."
        )


def _check_scope(statement: exp.Expression, conn: ConnectionContext) -> list[TableRef]:
    cte_names = {cte.alias_or_name.casefold() for cte in statement.find_all(exp.CTE)}
    allowed_schemas = {schema.casefold() for schema in conn.schemas}
    catalog = conn.catalog.casefold()
    tables: list[TableRef] = []

    for table in statement.find_all(exp.Table):
        name, schema, table_catalog = table.name, table.db, table.catalog
        if not schema and not table_catalog and name.casefold() in cte_names:
            continue  # a reference to a CTE defined in this query
        if not schema:
            schema = conn.default_schema or ""
            if not schema:
                raise QueryRejected(
                    f"Qualify {name} with its schema ({', '.join(conn.schemas)})."
                )
        ref = TableRef(table_catalog, schema, name)
        if table_catalog and table_catalog.casefold() != catalog:
            raise QueryRejected(f"{ref} is outside this connection (catalog {conn.catalog}).")
        if schema.casefold() not in allowed_schemas:
            raise QueryRejected(
                f"{ref} is outside this connection. Allowed schemas: {', '.join(conn.schemas)}."
            )
        tables.append(ref)
    return tables


def resolve_table_name(conn: ConnectionContext, dotted: str) -> tuple[str, str]:
    """
    "table", "schema.table" or "catalog.schema.table" -> (schema, table), refusing
    anything outside the connection's scope.
    """
    parts = [part.strip() for part in dotted.strip().split(".")]
    if not 1 <= len(parts) <= 3 or not all(parts):
        raise QueryRejected("Name the table as table, schema.table or catalog.schema.table.")
    if len(parts) == 3 and parts[0].casefold() != conn.catalog.casefold():
        raise QueryRejected(f"{dotted} is outside this connection (catalog {conn.catalog}).")
    name = parts[-1]
    schema = parts[-2] if len(parts) >= 2 else (conn.default_schema or "")
    if not schema:
        raise QueryRejected(f"Qualify {name} with its schema ({', '.join(conn.schemas)}).")
    check_object(conn, schema)
    return schema, name


def check_object(conn: ConnectionContext, schema: str) -> None:
    """For list/describe tools: is this schema inside the connection's scope?"""
    if schema.casefold() not in {s.casefold() for s in conn.schemas}:
        raise QueryRejected(
            f"Schema {schema} is outside this connection. Allowed: {', '.join(conn.schemas)}."
        )


MAX_DAX_LENGTH = 100_000
_LINE_COMMENT = re.compile(r"(--|//)[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_DAX_FORBIDDEN = ("createorreplace", "alter", "delete", "refresh", "backup")


def check_dax(dax: str) -> str:
    raw = dax.strip()
    if not raw:
        raise QueryRejected("The DAX query is empty.")
    if len(raw) > MAX_DAX_LENGTH:
        raise QueryRejected("The DAX query is too long.")
    body = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", raw)).strip()
    if not re.match(r"^(evaluate|define)\b", body, re.I):
        raise QueryRejected("DAX must start with EVALUATE (or DEFINE ... EVALUATE). "
                            "Model-changing statements are not allowed.")
    for word in _DAX_FORBIDDEN:
        if re.search(rf"\b{word}\b", body, re.I):
            raise QueryRejected(f"DAX rejected: '{word}' is not allowed.")
    return raw
