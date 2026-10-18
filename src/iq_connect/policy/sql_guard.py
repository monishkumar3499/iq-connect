"""
The read-only SQL policy: decides whether a query may run, before any platform
sees it. One of several layers (see architecture section 10) -- queries also
run as the person, under their own platform permissions.

It is an allow-list, never a block-list, because there are endless ways to
phrase something harmful and only a few shapes of a harmless query:

  * exactly one statement, which SQL parsing must fully understand;
  * that statement is a SELECT (or UNION / INTERSECT / EXCEPT of SELECTs);
  * nothing anywhere inside it writes, locks, changes settings or runs code;
  * every FROM / JOIN source is a named table, a subquery or a CTE -- never a
    table function (file readers, result scans, OPENROWSET and the like);
  * every named table is inside the connection's scope.

Anything sqlglot cannot parse is refused: "we don't understand it" must mean
"no", never "probably fine".

Roadmap step 7 hardens this with an adversarial suite per dialect and
dialect-specific function rules.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ..adapters.base import ConnectionContext

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
    return _check_scope(statement, conn)


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
