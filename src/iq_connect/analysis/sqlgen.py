"""
SQL the server writes for itself (row counts, profiles, samples), rendered for
each platform's dialect by sqlglot -- `TOP` for Fabric, `LIMIT` elsewhere,
the right identifier quoting everywhere. Names come from the catalogue, never
from the caller, and are quoted; the result still goes through the same
read-only policy as Claude's own queries.
"""

from __future__ import annotations

from sqlglot import exp


def table_ref(schema: str, name: str, dialect: str) -> str:
    return exp.table_(name, db=schema, quoted=True).sql(dialect=dialect)


def ident(name: str, dialect: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def literal(text: str, dialect: str) -> str:
    return exp.Literal.string(text).sql(dialect=dialect)


def row_counts(tables: list[tuple[str, str]], dialect: str) -> str:
    """One statement counting every table: (qualified name, rows) per table."""
    parts = [
        f"SELECT {literal(f'{schema}.{name}', dialect)} AS t, COUNT(*) AS n "
        f"FROM {table_ref(schema, name, dialect)}"
        for schema, name in tables
    ]
    return " UNION ALL ".join(parts)


def profile(schema: str, name: str, keys: list[str], dates: list[str], dialect: str) -> str:
    """COUNT(*), then per key: distinct + nulls, then per date: min + max."""
    selects = ["COUNT(*)"]
    for key in keys:
        column = ident(key, dialect)
        selects.append(f"COUNT(DISTINCT {column})")
        selects.append(f"SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END)")
    for date in dates:
        column = ident(date, dialect)
        selects += [f"MIN({column})", f"MAX({column})"]
    return f"SELECT {', '.join(selects)} FROM {table_ref(schema, name, dialect)}"


def sample(schema: str, name: str, rows: int, dialect: str) -> str:
    statement = exp.select("*").from_(exp.table_(name, db=schema, quoted=True)).limit(rows)
    return statement.sql(dialect=dialect)
