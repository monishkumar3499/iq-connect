"""Finding and creating a person's named connections."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..adapters.base import ConnectionContext
from ..db.models import DataConnection, User


class ConnectionNotFound(Exception):
    pass


def list_for_user(session: Session, user_id: uuid.UUID) -> list[DataConnection]:
    return list(session.scalars(
        select(DataConnection)
        .where(DataConnection.user_id == user_id)
        .order_by(DataConnection.name)
    ))


def find_by_name(session: Session, user_id: uuid.UUID, name: str) -> DataConnection:
    """
    The person's connection with this name (case-insensitive). Always filtered
    by the person: a name never reaches another person's connection, even if
    both have one called "Sales".
    """
    row = session.scalars(
        select(DataConnection).where(
            DataConnection.user_id == user_id,
            func.lower(DataConnection.name) == name.strip().lower(),
        )
    ).first()
    if row is None:
        raise ConnectionNotFound(name)
    return row


def context_of(row: DataConnection) -> ConnectionContext:
    credential_version = row.credential.version if row.credential is not None else None
    return ConnectionContext(
        id=str(row.id), name=row.name, platform=row.platform,
        scope=dict(row.scope), settings=dict(row.settings),
        credential_version=credential_version,
    )


def create(session: Session, user: User, *, platform: str, name: str,
           scope: dict[str, Any], custom_prompt: str = "",
           settings: dict[str, Any] | None = None,
           credential_id: uuid.UUID | None = None) -> DataConnection:
    row = DataConnection(
        org_id=user.org_id, user_id=user.id, platform=platform, name=name.strip(),
        scope=scope, custom_prompt=custom_prompt, settings=settings or {},
        credential_id=credential_id,
    )
    session.add(row)
    session.flush()
    return row
