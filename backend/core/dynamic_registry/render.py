"""Turning results into answers for Claude, and the signed handles for long-running queries."""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer

from backend.core.dynamic_registry.base import Rows
from backend.core.security.policy import QueryRejected

MAX_CELL = 200


def cell(value: Any, max_cell: int = MAX_CELL) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<binary, {len(bytes(value))} bytes>"
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, float | decimal.Decimal):
        text = format(value, "f") if isinstance(value, decimal.Decimal) else repr(value)
    else:
        text = str(value)
    # Pipes and line breaks would break the table layout.
    text = text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    # Plain ASCII "...": tool output stays ASCII so every client renders it.
    return text if len(text) <= max_cell else text[: max_cell - 3] + "..."


def table(rows: Rows, max_chars: int, max_cell: int = MAX_CELL) -> str:
    if not rows.columns:
        return "The query returned no columns."
    header = "| " + " | ".join(cell(c) for c in rows.columns) + " |"
    divider = "|" + "|".join("---" for _ in rows.columns) + "|"
    lines = [header, divider]
    used = len(header) + len(divider)
    shown = 0
    for row in rows.rows:
        line = "| " + " | ".join(cell(v, max_cell) for v in row) + " |"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1

    notes = list(rows.notes)
    if shown < len(rows.rows):
        notes.append(f"Showing {shown} of {len(rows.rows)} rows (output size limit). "
                     "Select fewer columns or aggregate.")
    elif rows.truncated:
        notes.append(f"Showing the first {shown} rows; more exist. Raise max_rows or aggregate.")
    else:
        notes.append(f"{shown} row{'s' if shown != 1 else ''}.")
    return "\n".join(lines) + "\n\n" + " ".join(notes)


# How long a handle works. The platform may discard a result sooner; the
# adapter then says so.
HANDLE_SECONDS = 3600


class BadHandle(QueryRejected):
    """Not a query id this person may use on this connection (or it expired)."""


class QueryHandles:
    def __init__(self, keys: list[bytes]) -> None:
        self._serializer = URLSafeTimedSerializer(keys, salt="iq-query-handle")

    def issue(self, *, user_id: str, connection_id: str, query_id: str) -> str:
        return self._serializer.dumps({"u": user_id, "c": connection_id, "q": query_id})

    def open(self, handle: str, *, user_id: str, connection_id: str) -> str:
        """The platform's statement id, or BadHandle."""
        try:
            data = self._serializer.loads(handle.strip(), max_age=HANDLE_SECONDS)
        except BadSignature:  # includes SignatureExpired
            raise BadHandle("This query id is unknown or has expired. Run the query "
                            "again.") from None
        if not isinstance(data, dict) or not isinstance(data.get("q"), str):
            raise BadHandle("This query id is unknown. Run the query again.")
        if data.get("u") != user_id or data.get("c") != connection_id:
            raise BadHandle("This query id belongs to another connection. Use it with "
                            "the connection the query ran on.")
        return data["q"]
