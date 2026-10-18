"""
Profiling one table: grain, key uniqueness, fan-out, null keys, time span and
a sample -- the evidence step that keeps an aggregate honest.

Ported from the prototype's `warehouse_profile` (`fabric_iq/app.py`), made
platform-neutral: SQL comes from `sqlgen` in the adapter's dialect and runs
through a caller-supplied `run(sql, limit)` that applies the read-only policy.

Declared keys are a statement of intent, never proof (Fabric accepts PRIMARY
KEY only as NOT ENFORCED; Databricks constraints are informational), so
uniqueness and fan-out are measured, not assumed.
"""

from __future__ import annotations

from collections.abc import Callable

from ..adapters.base import Rows
from ..mcp import render
from . import sqlgen
from .schema_map import Table

Run = Callable[[str, int], Rows]

# Exact COUNT DISTINCT per key column; more than this would make one call slow.
KEY_LIMIT = 4


def profile_table(table: Table, dialect: str, run: Run, *, sample_rows: int, checks: bool,
                  max_chars: int) -> str:
    lines = [f"## {table.qualified} {table.kind}"]
    keys = [c.name for c in table.key_columns if not c.is_audit][:KEY_LIMIT]
    date_columns = table.date_columns[:2]
    dates = [c.name for c in date_columns]

    stats: list = []
    if checks:
        result = run(sqlgen.profile(table.schema, table.name, keys, dates, dialect), 1)
        stats = list(result.rows[0]) if result.rows else []

    total = _int(stats[0]) if stats else None
    if total is not None:
        lines[0] += f" | {total} rows"

    if not checks:
        lines.append("_checks=false: grain, key cardinality and null coverage were not "
                     "measured. Do not aggregate across a join on this basis alone._")
    elif not total:
        lines.append("EMPTY TABLE -- 0 rows. This is a finding to report, not a query "
                     "problem. Check whether the load is stale before using it.")
    else:
        offset, unique, notes = 1, [], []
        for key in keys:
            distinct, nulls = _int(stats[offset]), _int(stats[offset + 1])
            offset += 2
            note = f"{key}: {distinct} distinct"
            if distinct == total and not nulls:
                unique.append(key)
                note += " (UNIQUE, candidate key)"
            elif distinct and distinct < total:
                note += f", ~{total / distinct:.1f} rows per value (many side)"
            if nulls:
                # A tiny share still vanishes from an INNER JOIN; say the count.
                share = nulls / total * 100
                note += (f", {nulls} NULL" + (f" ({share:.1f}%)" if share >= 0.05 else "")
                         + " -> those rows are dropped by an INNER JOIN on this key")
            notes.append(note)
        if unique:
            lines.append(f"Grain: UNIQUE on {unique[0]} ({total} distinct over {total} rows) "
                         f"-> one row per {unique[0]}; aggregating this table alone cannot "
                         "double count.")
        elif keys:
            lines.append(f"Grain: NOT unique on any of the {len(keys)} key columns checked -> "
                         "the grain is composite or the table holds history. Confirm it with "
                         "GROUP BY <suspected key> HAVING COUNT(*) > 1 before you SUM "
                         "anything through a join.")
        else:
            lines.append("Grain: no identifier-shaped column found, so grain could not be "
                         "measured. Establish it explicitly before aggregating.")
        if table.declared_pk:
            lines.append(f"Declared PK: {', '.join(table.declared_pk)}")
        if notes:
            lines.append("Keys: " + " | ".join(notes))
        for index, column in enumerate(date_columns):
            low, high = stats[offset + index * 2], stats[offset + index * 2 + 1]
            label = "load/effective date" if column.is_audit else "business date"
            lines.append(f"Time ({column.name}, {label}): {render.cell(low)} -> "
                         f"{render.cell(high)}")

    lines.append("Columns: " + "; ".join(
        f"{c.name} {c.type_text}" + ("" if c.nullable else "!") for c in table.columns))
    if sample_rows:
        sample = run(sqlgen.sample(table.schema, table.name, sample_rows, dialect), sample_rows)
        if sample.columns:
            lines.append(f"Sample ({len(sample.rows)}):")
            lines.append(render.table(Rows(sample.columns, sample.rows), max_chars).rsplit(
                "\n\n", 1)[0])
    return "\n".join(lines)


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return int(float(value))
