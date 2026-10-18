"""
The read-only DAX policy. Ported from the prototype (`fabric_iq/core.py::guard_dax`).

The Power BI Execute Queries API is read-only by contract; this adds an
explicit shape check so malformed or model-changing payloads fail fast and
locally, before any token is minted or request sent. DEFINE MEASURE is
allowed: it is scoped to the one query and never changes the published model.
"""

from __future__ import annotations

import re

from .sql_guard import QueryRejected

MAX_DAX_LENGTH = 100_000
_LINE_COMMENT = re.compile(r"(--|//)[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_FORBIDDEN = ("createorreplace", "alter", "delete", "refresh", "backup")


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
    for word in _FORBIDDEN:
        if re.search(rf"\b{word}\b", body, re.I):
            raise QueryRejected(f"DAX rejected: '{word}' is not allowed.")
    return raw
