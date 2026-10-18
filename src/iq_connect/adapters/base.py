"""
The interface every data platform implements -- same calls, different insides.

The core (pipeline, policy, audit) only ever talks to this interface, so it
never knows which platform it is driving. Adapters are plain synchronous code:
the core runs them on the platform's own worker threads, with timeouts.

Scope, the same shape for every platform:
    {"catalog": "<Fabric warehouse | Databricks catalog | Snowflake database>",
     "schemas": ["<schema>", ...],
     "default_schema": "<schema used for unqualified table names>"}   (optional)
The core checks every table a query names against it before an adapter runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class AdapterError(Exception):
    """A failure the person can act on (bad SQL, missing table). Shown to Claude."""


class CredentialRejected(AdapterError):
    """
    The platform refused the stored login (wrong or changed password, disabled
    user). The pipeline marks every connection using that login as needing
    sign-in, so later calls say so at once instead of failing at the platform.
    """


@dataclass(frozen=True)
class ConnectionContext:
    """What an adapter needs to know about the connection it serves right now."""

    id: str
    name: str
    platform: str
    scope: dict[str, Any]
    settings: dict[str, Any]
    credential_version: int | None

    @property
    def catalog(self) -> str:
        return str(self.scope.get("catalog", ""))

    @property
    def schemas(self) -> list[str]:
        return [str(s) for s in self.scope.get("schemas", [])]

    @property
    def default_schema(self) -> str | None:
        explicit = self.scope.get("default_schema")
        if explicit:
            return str(explicit)
        return self.schemas[0] if len(self.schemas) == 1 else None


@dataclass(frozen=True)
class DbObject:
    schema: str
    name: str
    kind: str  # table | view


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool = True
    comment: str = ""


@dataclass(frozen=True)
class TableInfo:
    schema: str
    name: str
    kind: str
    columns: list[Column]
    row_estimate: int | None = None
    comment: str = ""


@dataclass(frozen=True)
class ColumnMeta:
    """One column of one table, as the catalogue describes it."""

    schema: str
    table: str
    kind: str  # TABLE | VIEW
    column: str
    data_type: str
    nullable: bool
    ordinal: int


@dataclass
class CatalogMeta:
    """
    Everything `data_map` needs, for the connection's schemas only. Declared
    keys are optional: many warehouses declare none, and the map then infers
    them from naming (and says so).
    """

    columns: list[ColumnMeta] = field(default_factory=list)
    # (schema, table, column) per primary-key or unique column
    primary_keys: list[tuple[str, str, str]] = field(default_factory=list)
    # (from_schema, from_table, from_column, to_schema, to_table, to_column)
    foreign_keys: list[tuple[str, str, str, str, str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Rows:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool = False
    notes: list[str] = field(default_factory=list)


class PlatformAdapter(Protocol):
    platform: str
    label: str
    # sqlglot dialect name, used by the read-only SQL policy.
    dialect: str
    # e.g. {"sql"} or {"sql", "semantic_model"}
    capabilities: frozenset[str]

    def list_objects(
        self, conn: ConnectionContext, secret: dict[str, Any] | None, schema: str | None
    ) -> list[DbObject]: ...

    def describe_table(
        self, conn: ConnectionContext, secret: dict[str, Any] | None, schema: str, table: str
    ) -> TableInfo: ...

    def run_query(
        self, conn: ConnectionContext, secret: dict[str, Any] | None, sql: str, limit: int
    ) -> Rows: ...

    def catalog(self, conn: ConnectionContext, secret: dict[str, Any] | None) -> CatalogMeta:
        """Columns, declared keys and foreign keys of the connection's schemas."""
        ...
