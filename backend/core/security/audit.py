"""One audit row per tool call: who, what, where, how it went."""

from __future__ import annotations

import logging
import uuid

from backend.shared.database import Database
from backend.shared.models import AuditEvent

log = logging.getLogger(__name__)

# Keep the record useful without letting one huge statement bloat the table.
MAX_STATEMENT = 20_000
MAX_ERROR = 2_000


def record(
    db: Database,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    tool: str,
    outcome: str,
    duration_ms: int,
    connection_id: uuid.UUID | None = None,
    platform: str | None = None,
    statement: str | None = None,
    row_count: int | None = None,
    error: str | None = None,
) -> None:
    """
    Write the audit row. A failure to audit is logged loudly but never turns a
    successful query into an error: the person already has the answer, and the
    log line itself is a record of the call.
    """
    try:
        with db.session() as session:
            session.add(AuditEvent(
                org_id=org_id, user_id=user_id, connection_id=connection_id,
                platform=platform, tool=tool,
                statement=statement[:MAX_STATEMENT] if statement else None,
                row_count=row_count, duration_ms=duration_ms, outcome=outcome,
                error=error[:MAX_ERROR] if error else None,
            ))
    except Exception:
        log.exception("AUDIT WRITE FAILED for user %s tool %s (%s)", user_id, tool, outcome)
