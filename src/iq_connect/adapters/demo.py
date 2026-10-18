"""
A built-in demo platform: a small sales database in memory (SQLite).

It lets the whole path -- Claude, sign-in, connection, policy, audit -- run
end to end before any real warehouse is connected, and gives the tests a real
SQL engine. Each call gets a fresh, read-only copy, so nothing can change it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from .base import (
    AdapterError,
    CatalogMeta,
    Column,
    ColumnMeta,
    ConnectionContext,
    DbObject,
    Rows,
    TableInfo,
)

SCHEMA = "sales"

_TABLES = {
    "customers": (
        "CREATE TABLE sales.customers (customer_id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "country TEXT NOT NULL, segment TEXT NOT NULL)",
        [
            (1, "Northwind Traders", "India", "Enterprise"),
            (2, "Contoso Ltd", "United Kingdom", "Enterprise"),
            (3, "Fabrikam Inc", "United States", "Mid-market"),
            (4, "Tailspin Toys", "India", "Small business"),
            (5, "Wide World Importers", "Germany", "Mid-market"),
        ],
    ),
    "orders": (
        "CREATE TABLE sales.orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, "
        "order_date TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL)",
        [
            (101, 1, "2026-07-03", 1250.00, "shipped"),
            (102, 2, "2026-07-11", 830.50, "shipped"),
            (103, 1, "2026-08-02", 2100.00, "shipped"),
            (104, 3, "2026-08-19", 455.25, "cancelled"),
            (105, 4, "2026-09-01", 310.00, "shipped"),
            (106, 5, "2026-09-14", 1780.75, "processing"),
            (107, 2, "2026-09-28", 995.00, "shipped"),
            (108, 1, "2026-10-05", 640.00, "processing"),
        ],
    ),
}


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(f"ATTACH DATABASE ':memory:' AS {SCHEMA}")
    for create, rows in _TABLES.values():
        connection.execute(create)
        table = create.split()[2]
        marks = ", ".join("?" * len(rows[0]))
        connection.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
    connection.commit()
    # Belt and braces: the policy only lets SELECTs through, and SQLite
    # refuses any write on this connection as well.
    connection.execute("PRAGMA query_only = ON")
    return connection


class DemoAdapter:
    platform = "demo"
    label = "Demo (sample sales data)"
    dialect = "sqlite"
    capabilities = frozenset({"sql"})

    def list_objects(
        self, conn: ConnectionContext, secret: dict[str, Any] | None, schema: str | None
    ) -> list[DbObject]:
        return [DbObject(SCHEMA, name, "table") for name in sorted(_TABLES)]

    def describe_table(
        self, conn: ConnectionContext, secret: dict[str, Any] | None, schema: str, table: str
    ) -> TableInfo:
        if schema.lower() != SCHEMA or table.lower() not in _TABLES:
            raise AdapterError(f"No table {schema}.{table}")
        with closing(_database()) as db:
            columns = [
                Column(name=row[1], type=row[2], nullable=not row[3])
                for row in db.execute(f"PRAGMA {SCHEMA}.table_info({table.lower()})")
            ]
        return TableInfo(SCHEMA, table.lower(), "table", columns,
                         row_estimate=len(_TABLES[table.lower()][1]))

    def run_query(
        self, conn: ConnectionContext, secret: dict[str, Any] | None, sql: str, limit: int
    ) -> Rows:
        with closing(_database()) as db:
            try:
                cursor = db.execute(sql)
            except sqlite3.Error as error:
                raise AdapterError(f"SQLite: {error}") from None
            columns = [d[0] for d in cursor.description or []]
            fetched = cursor.fetchmany(limit + 1)
        return Rows(columns, [tuple(r) for r in fetched[:limit]], truncated=len(fetched) > limit)

    def catalog(self, conn: ConnectionContext, secret: dict[str, Any] | None) -> CatalogMeta:
        meta = CatalogMeta()
        with closing(_database()) as db:
            for table in sorted(_TABLES):
                for cid, name, kind, notnull, _, pk in db.execute(
                    f"PRAGMA {SCHEMA}.table_info({table})"
                ):
                    meta.columns.append(ColumnMeta(SCHEMA, table, "TABLE", name, kind,
                                                   not notnull and not pk, cid + 1))
                    if pk:
                        meta.primary_keys.append((SCHEMA, table, name))
        return meta
