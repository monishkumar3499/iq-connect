"""
A person's platform accounts (`PlatformAccount` rows): which Databricks workspaces,
Snowflake accounts or Fabric sign-in they use, and how they sign in to each. Written by the
setup's Sign in step; read by the setup drivers. Secrets are never here -- only the id of
the credential that holds them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.shared.database import Database
from backend.shared.models import PlatformAccount


@dataclass(frozen=True)
class AccountInfo:
    id: uuid.UUID | None  # None: implied by the server's defaults, not saved by the person
    platform: str
    address: str
    method: str
    options: dict[str, Any] = field(default_factory=dict)
    credential_id: uuid.UUID | None = None


def _info(row: PlatformAccount) -> AccountInfo:
    return AccountInfo(row.id, row.platform, row.address, row.method, dict(row.options or {}),
                       row.credential_id)


def list_accounts(db: Database, user_id: uuid.UUID, platform: str) -> list[AccountInfo]:
    with db.session() as session:
        rows = session.scalars(select(PlatformAccount).where(
            PlatformAccount.user_id == user_id, PlatformAccount.platform == platform)
            .order_by(PlatformAccount.updated_at.desc()))
        return [_info(row) for row in rows]


def find_account(db: Database, user_id: uuid.UUID, platform: str,
                 address: str) -> AccountInfo | None:
    with db.session() as session:
        row = session.scalars(select(PlatformAccount).where(
            PlatformAccount.user_id == user_id, PlatformAccount.platform == platform,
            PlatformAccount.address == address)).one_or_none()
        return _info(row) if row else None


def account_by_id(db: Database, user_id: uuid.UUID, account_id: str) -> AccountInfo | None:
    """The person's own account with this id; another person's id finds nothing."""
    try:
        parsed = uuid.UUID(str(account_id))
    except ValueError:
        return None
    with db.session() as session:
        row = session.get(PlatformAccount, parsed)
        return _info(row) if row is not None and row.user_id == user_id else None


def save_account(session: Session, *, user_id: uuid.UUID, platform: str, address: str,
                 method: str, options: dict[str, Any] | None = None,
                 credential_id: uuid.UUID | None = None) -> uuid.UUID:
    """Create or update the person's account for (platform, address)."""
    row = session.scalars(select(PlatformAccount).where(
        PlatformAccount.user_id == user_id, PlatformAccount.platform == platform,
        PlatformAccount.address == address)).one_or_none()
    if row is None:
        row = PlatformAccount(user_id=user_id, platform=platform, address=address)
        session.add(row)
    row.method, row.options, row.credential_id = method, dict(options or {}), credential_id
    session.flush()
    return row.id


def attach_credential(db: Database, account_id: uuid.UUID, credential_id: uuid.UUID) -> None:
    """After a platform sign-in completes: the account now signs in with this credential."""
    with db.session() as session:
        row = session.get(PlatformAccount, account_id)
        if row is not None:
            row.credential_id = credential_id
