"""
Turning catalogue metadata into something a model can act on in one read.

Ported from the prototype's `fabric_iq/schema.py` (which worked well there),
made platform-neutral: the input is an adapter's `CatalogMeta`, so Fabric,
Databricks and any later platform get the same map.

INFORMATION_SCHEMA answers "what columns exist". It does not answer the three
questions that decide whether a KPI query is correct:

    which column identifies a row here?      (grain)
    how does this table join to that one?    (keys)
    what modelling pattern am I looking at?  (star / snowflake / vault)

This module answers all three from metadata already in hand. Declared
constraints are used where a warehouse has them; many declare none (Fabric
does not even enforce them), so name-shape inference is the fallback and is
always labelled as inference so the model can tell the two apart.

Everything here is pure: metadata in, text out. No I/O, no policy.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..adapters.base import CatalogMeta

# Prefixes that carry modelling meaning; longest first so `fct_` wins over `f_`.
_FACT_PREFIXES = ("fact_", "fct_", "f_")
_DIM_PREFIXES = ("dim_", "dm_", "d_")
_VAULT_PREFIXES = {
    "hub_": "hub", "lnk_": "link", "link_": "link", "sat_": "satellite", "s_": "satellite",
    "pit_": "pit", "bridge_": "bridge", "br_": "bridge",
}
_NOISE_PREFIXES = ("vw_", "v_", "tbl_")

# Column-name shapes that denote an identifier rather than an attribute.
_KEY_SUFFIXES = ("_key", "_id", "_sk", "_bk", "_hk", "_code", "_no", "_num", "_nbr")
_KEY_EXACT = ("id", "key", "code", "sk", "pk")

# Load metadata, called out because mistaking `load_date` for the business date
# silently answers the wrong question.
_AUDIT_HINTS = (
    "load_date", "load_dts", "loaddate", "record_source", "rec_src",
    "effective_from", "effective_to", "valid_from", "valid_to",
    "_inserted", "_updated", "etl_", "dw_", "hash_diff", "hashdiff",
    "is_current", "row_is_current", "_version",
)

# Type families across T-SQL, Databricks SQL, Snowflake and SQLite.
DATE_TYPES = {
    "date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "timestamp",
    "timestamp_ntz", "timestamp_ltz", "timestamp_tz", "time",
}
NUMERIC_TYPES = {
    "int", "integer", "bigint", "smallint", "tinyint", "long", "short", "byte",
    "decimal", "numeric", "number", "float", "double", "real", "money", "smallmoney",
}


def base_type(data_type: str) -> str:
    """'decimal(18,2)' -> 'decimal', 'TIMESTAMP_NTZ' -> 'timestamp_ntz'."""
    return re.split(r"[\s(<]", str(data_type).strip().lower(), maxsplit=1)[0]


@dataclass
class Column:
    name: str
    type_text: str
    nullable: bool
    position: int = 0

    @property
    def is_key_like(self) -> bool:
        lowered = self.name.lower()
        return lowered in _KEY_EXACT or lowered.endswith(_KEY_SUFFIXES)

    @property
    def is_date(self) -> bool:
        return base_type(self.type_text) in DATE_TYPES

    @property
    def is_numeric(self) -> bool:
        return base_type(self.type_text) in NUMERIC_TYPES

    @property
    def is_audit(self) -> bool:
        lowered = self.name.lower()
        return any(hint in lowered for hint in _AUDIT_HINTS)

    @property
    def is_measure_like(self) -> bool:
        """Additive-looking: numeric, not an identifier, not load metadata."""
        return self.is_numeric and not self.is_key_like and not self.is_audit


@dataclass
class Table:
    schema: str
    name: str
    kind: str  # TABLE | VIEW
    columns: list[Column] = field(default_factory=list)
    row_count: Any = None
    declared_pk: list[str] = field(default_factory=list)
    # column -> (schema, table, column); declared FKs first, then name matching.
    edges: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    declared_edges: set[str] = field(default_factory=set)
    inferred_pk: list[str] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def key_columns(self) -> list[Column]:
        return [column for column in self.columns if column.is_key_like]

    @property
    def date_columns(self) -> list[Column]:
        """Business dates first, load/effective dates last."""
        return sorted((c for c in self.columns if c.is_date), key=lambda c: c.is_audit)

    @property
    def measure_columns(self) -> list[Column]:
        return [column for column in self.columns if column.is_measure_like]

    @property
    def primary_key(self) -> list[str]:
        return self.declared_pk or self.inferred_pk

    @property
    def role(self) -> str:
        """fact / dim / hub / link / satellite / pit / bridge / unknown."""
        lowered = self.name.lower()
        for prefix, role in _VAULT_PREFIXES.items():
            # `s_` and `d_` collide with real words; they only count when the
            # rest of the name looks like an entity.
            if lowered.startswith(prefix) and (
                len(prefix) > 2 or "_" in lowered[len(prefix):]
                or len(lowered) > len(prefix) + 2
            ):
                return role
        if lowered.startswith(_FACT_PREFIXES) or self.schema.lower() in ("fact", "facts"):
            return "fact"
        if lowered.startswith(_DIM_PREFIXES) or self.schema.lower() in ("dim", "dims", "dimension"):
            return "dim"
        return "unknown"

    @property
    def stem(self) -> str:
        """Entity name with role prefixes and view noise removed."""
        lowered = self.name.lower()
        for prefix in (*_VAULT_PREFIXES, *_FACT_PREFIXES, *_DIM_PREFIXES, *_NOISE_PREFIXES):
            if lowered.startswith(prefix):
                return lowered[len(prefix):]
        return lowered


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def build(meta: CatalogMeta) -> dict[str, Table]:
    tables: dict[str, Table] = {}
    for c in meta.columns:
        key = f"{c.schema}.{c.table}"
        table = tables.get(key)
        if table is None:
            table = tables[key] = Table(c.schema, c.table,
                                        "VIEW" if c.kind.upper() == "VIEW" else "TABLE")
        table.columns.append(Column(c.column, c.data_type, c.nullable, c.ordinal))
    for table in tables.values():
        table.columns.sort(key=lambda column: column.position)

    for schema, name, column in meta.primary_keys:
        table = tables.get(f"{schema}.{name}")
        if table is not None and column not in table.declared_pk:
            table.declared_pk.append(column)
    for fs, ft, fc, ts, tt, tc in meta.foreign_keys:
        table = tables.get(f"{fs}.{ft}")
        if table is not None:
            table.edges[fc] = (ts, tt, tc)
            table.declared_edges.add(fc)
    infer_keys(tables)
    return tables


def infer_keys(tables: dict[str, Table]) -> None:
    """
    Fill in primary keys and join edges the warehouse never declared.

    A primary-key candidate is a NOT NULL identifier column whose stem matches
    the table's own entity name (`dim_customer.customer_key`), or a column
    plainly named `id`, or the table's single identifier column. Anything less
    clear-cut is left empty rather than guessed -- an invented key is worse
    than no key, because it reads as verified.

    A join edge is an identifier column that is some *other* table's key of the
    same name. Dimension targets win ties: a fact column matching both a
    dimension key and another fact's key almost always points at the dimension.
    """
    for table in tables.values():
        if table.declared_pk:
            continue
        keys = [column for column in table.key_columns if not column.is_audit]
        stems = {table.stem, table.stem.rstrip("s"), table.stem + "s"}
        matched = [c for c in keys if not c.nullable
                   and any(c.name.lower().startswith(s) for s in stems if s)]
        if len(matched) == 1:
            table.inferred_pk = [matched[0].name]
        elif not matched:
            exact = [c for c in keys if c.name.lower() == "id" and not c.nullable]
            not_null = [c for c in keys if not c.nullable]
            if exact:
                table.inferred_pk = [exact[0].name]
            elif len(not_null) == 1:
                table.inferred_pk = [not_null[0].name]

    key_owners: dict[str, list[Table]] = {}
    for table in tables.values():
        for name in {*table.declared_pk, *table.inferred_pk}:
            key_owners.setdefault(name.lower(), []).append(table)

    for table in tables.values():
        for column in table.key_columns:
            if column.name in table.edges:
                continue  # a declared FK already answers this
            owners = [o for o in key_owners.get(column.name.lower(), [])
                      if o.qualified != table.qualified]
            if not owners:
                continue
            owners.sort(key=lambda o: (o.role != "dim", o.role != "hub", o.qualified))
            target = owners[0]
            if len(owners) > 1 and target.role == owners[1].role:
                continue  # genuinely ambiguous -- say nothing rather than pick
            table.edges[column.name] = (target.schema, target.name, column.name)


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------


def detect_pattern(tables: dict[str, Table]) -> tuple[str, str]:
    """
    Name the modelling pattern and return the advice that follows from it:
    "there are PIT tables, use them instead of raw satellites" is a correctness
    instruction the model cannot derive from a column list.
    """
    roles: dict[str, int] = {}
    for table in tables.values():
        roles[table.role] = roles.get(table.role, 0) + 1
    facts, dims = roles.get("fact", 0), roles.get("dim", 0)
    hubs, links, sats = roles.get("hub", 0), roles.get("link", 0), roles.get("satellite", 0)
    pits, bridges = roles.get("pit", 0), roles.get("bridge", 0)
    vault, star = hubs + links + sats + pits + bridges, facts + dims

    # Snowflaking shows up as a dimension keyed to another dimension.
    snowflake_edges = sorted({
        f"{table.qualified}->{target[0]}.{target[1]}"
        for table in tables.values() if table.role == "dim"
        for target in table.edges.values()
        if tables.get(f"{target[0]}.{target[1]}", Table("", "", "")).role == "dim"
    })

    if vault >= 2 and star >= 2:
        headline = "MIXED (Data Vault + dimensional)"
    elif vault >= 2:
        headline = "DATA VAULT"
    elif snowflake_edges:
        headline = "SNOWFLAKE"
    elif star >= 2:
        headline = "STAR"
    else:
        headline = "UNCLEAR"

    parts: list[str] = []
    if star:
        parts.append(f"{facts} fact / {dims} dim")
    if vault:
        parts.append(", ".join(f"{n} {label}" for label, n in (
            ("hub", hubs), ("link", links), ("sat", sats), ("pit", pits), ("bridge", bridges))
            if n))
    if roles.get("unknown"):
        parts.append(f"{roles['unknown']} unclassified")

    notes: list[str] = []
    if headline in ("SNOWFLAKE", "MIXED (Data Vault + dimensional)") and snowflake_edges:
        notes.append(
            "Normalised dimension chains present (" + ", ".join(snowflake_edges[:4])
            + ("..." if len(snowflake_edges) > 4 else "")
            + "): traverse the full chain for an attribute that is not on the dimension "
            "the fact joins to, and check cardinality at each hop.")
    if sats:
        notes.append(
            f"{pits} PIT / {bridges} bridge table(s) exist -- query those instead of "
            "filtering satellite history by hand." if pits or bridges else
            "Satellites with no PIT table: every satellite read needs an explicit "
            "current/as-of filter, or historical versions of one entity double-count.")
    if links:
        notes.append(f"{links} link table(s): treat each as a relationship, not an entity, "
                     "and confirm one-to-many vs many-to-many before aggregating through it.")
    if facts > 1:
        notes.append(f"{facts} fact tables at potentially different grains: never join two "
                     "facts directly -- aggregate each to a shared grain first.")
    if headline == "UNCLEAR":
        notes.append("Naming does not signal a known pattern. State your structural "
                     "assumption before querying and verify it with profile_tables.")
    detail = f" ({'; '.join(parts)})" if parts else ""
    return headline + detail, " ".join(notes)


# ---------------------------------------------------------------------------
# Filtering and rendering
# ---------------------------------------------------------------------------


def parse_filter(raw: str) -> list[str]:
    return [piece.strip().lower() for piece in (raw or "").split(",") if piece.strip()]


def matches(table: Table, patterns: Sequence[str]) -> bool:
    """A pattern may be qualified (`fact.*`), a bare schema, or a bare table."""
    from fnmatch import fnmatch

    if not patterns:
        return True
    qualified, schema, name = table.qualified.lower(), table.schema.lower(), table.name.lower()
    return any(fnmatch(qualified, p) or fnmatch(schema, p) or fnmatch(name, p)
               or fnmatch(qualified, f"*{p}*") for p in patterns)


LEGEND = (
    "Legend: `name type` per column. `!`=NOT NULL. `->tbl`=key join to that table's "
    "same-named key (`(d)`=declared FK, otherwise inferred from naming -- verify with "
    "profile_tables before trusting an aggregate)."
)


def _edge_suffix(table: Table, column: Column) -> str:
    target = table.edges.get(column.name)
    if not target:
        return ""
    schema, name, target_column = target
    tail = f"->{schema}.{name}"
    if target_column.lower() != column.name.lower():
        tail += f".{target_column}"
    return tail + ("(d)" if column.name in table.declared_edges else "")


def _column_text(table: Table, column: Column) -> str:
    return (f"{column.name} {column.type_text}" + ("" if column.nullable else "!")
            + _edge_suffix(table, column))


def _heading(table: Table) -> str:
    bits = [f"## {table.qualified} {table.kind}"]
    if table.row_count is not None:
        bits.append(f"{table.row_count} rows")
    if table.primary_key:
        bits.append(f"{'PK' if table.declared_pk else 'PK?'}: {', '.join(table.primary_key)}")
    return " | ".join(bits)


def render(tables: Sequence[Table], header: Sequence[str], full: bool) -> str:
    """`full` lists every column; otherwise keys, dates and measure candidates only."""
    lines = list(header)
    for table in tables:
        lines.append(_heading(table))
        if full:
            lines.append("; ".join(_column_text(table, c) for c in table.columns))
        else:
            keys = [c for c in table.key_columns if not c.is_audit]
            if keys:
                lines.append("keys: " + "; ".join(_column_text(table, c) for c in keys))
            if table.date_columns:
                lines.append("dates: " + "; ".join(
                    f"{c.name} {c.type_text}" for c in table.date_columns[:3]))
            if table.measure_columns:
                lines.append("measures: " + "; ".join(
                    f"{c.name} {c.type_text}" for c in table.measure_columns[:6])
                    + (" ..." if len(table.measure_columns) > 6 else ""))
            lines.append(f"({len(table.columns)} columns total)")
        lines.append("")
    return "\n".join(lines)


def render_best(tables: Sequence[Table], header: Sequence[str], char_budget: int,
                object_limit: int) -> tuple[str, bool]:
    """
    The full listing when it fits; otherwise the summary -- but only when the
    summary is actually shorter (on narrow tables it is not). Returns
    (text, was_full).
    """
    full = render(tables, header, full=True)
    if len(tables) <= object_limit and len(full) <= char_budget:
        return full, True
    summary = render(tables, header, full=False)
    return (full, True) if len(summary) >= len(full) else (summary, False)
