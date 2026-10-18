"""
Column types that behave the same on every database we may move to.

Rule for the whole schema: use only these and SQLAlchemy's generic types
(String, Text, Integer, Boolean, LargeBinary, Uuid, JSON). Nothing from a
dialect package (no JSONB, ARRAY, NVARCHAR(MAX)...). That is what keeps a move
from SQLite to PostgreSQL or SQL Server a settings change, not a rewrite.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """
    Stores UTC, always returns timezone-aware UTC.

    SQLite has no time zones and PostgreSQL's `timestamptz` converts to the
    session's zone, so "a datetime column" means something different on each.
    Storing naive UTC and re-attaching UTC on the way out gives one behaviour
    everywhere. Naive values are refused: their zone is a guess, and a guessed
    expiry time is a security bug.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime needs a timezone-aware datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


# A JSON object column whose in-place changes (`row.scope["schemas"] = [...]`)
# are noticed and saved. Plain JSON only saves when the whole value is replaced.
#
# Warning: keep the row in a variable while changing it. The session holds
# loaded rows only weakly, so `session.get(X, id).scope["k"] = v` on one line
# lets Python discard the row -- and the change -- before the commit. Only
# top-level keys are tracked; to change a nested value, assign the top-level
# key again.
JSONDict = MutableDict.as_mutable(JSON)


def utcnow() -> datetime:
    return datetime.now(UTC)
