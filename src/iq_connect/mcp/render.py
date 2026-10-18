"""
Turning results into text Claude reads well: markdown tables with bounded size.

Bounded because everything returned lands in Claude's context window: one
`SELECT *` on a wide table must not crowd out the conversation.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

from ..adapters.base import Rows

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
