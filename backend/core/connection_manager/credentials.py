"""
Storing and reading a person's platform sign-ins (encrypted), and using them outside a
tool call.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.core.auth.microsoft import EntraTokens
from backend.core.security.crypto import DecryptionError, Sealed, SecretBox
from backend.shared.database import Database
from backend.shared.models import Credential, DataConnection, User


def _context(credential: Credential) -> dict[str, str]:
    # Binds the secret to whose it is and what it is for. Moving the row to
    # another user, or swapping ciphertexts between rows, breaks decryption.
    return {
        "table": "credentials",
        "user_id": str(credential.user_id),
        "provider": credential.provider,
        "account": credential.account,
    }


def save_secret(
    session: Session,
    box: SecretBox,
    *,
    user: User,
    provider: str,
    account: str,
    secret: dict[str, Any],
    expires_at: datetime | None = None,
) -> Credential:
    """Create or replace this person's login for (provider, account)."""
    credential = session.scalars(
        select(Credential).where(
            Credential.user_id == user.id,
            Credential.provider == provider,
            Credential.account == account,
        )
    ).one_or_none()
    if credential is None:
        credential = Credential(user_id=user.id, provider=provider, account=account, version=1)
        session.add(credential)
    else:
        credential.version += 1

    sealed = box.seal(json.dumps(secret).encode(), _context(credential))
    credential.ciphertext = sealed.ciphertext
    credential.wrapped_key = sealed.wrapped_key
    credential.key_version = sealed.key_version
    credential.expires_at = expires_at
    session.flush()
    return credential


def refresh_secret(session: Session, box: SecretBox, credential: Credential,
                   secret: dict[str, Any]) -> None:
    """
    Store a renewed form of the same login (e.g. a rotated refresh token).

    Unlike save_secret, the version is NOT bumped: it is the same login, and
    the version keys caches of tokens and sessions that are still valid. A
    provider that rotates its refresh token on every use would otherwise make
    those caches useless.
    """
    sealed = box.seal(json.dumps(secret).encode(), _context(credential))
    credential.ciphertext = sealed.ciphertext
    credential.wrapped_key = sealed.wrapped_key
    credential.key_version = sealed.key_version
    session.flush()


def read_secret(box: SecretBox, credential: Credential) -> dict[str, Any]:
    sealed = Sealed(credential.ciphertext, credential.wrapped_key, credential.key_version)
    return json.loads(box.open(sealed, _context(credential)))


def save_link(session: Session, box: SecretBox, *, user: User, provider: str, account: str,
              secret: dict[str, Any]) -> Credential:
    """
    Store a platform link (a new sign-in) and bring back the connections that were
    waiting for it -- only those still attached to this same credential.
    """
    credential = save_secret(session, box, user=user, provider=provider, account=account,
                             secret=secret)
    session.execute(
        update(DataConnection)
        .where(DataConnection.credential_id == credential.id)
        .values(status="active")
    )
    return credential


def linked_usernames(session: Session, box: SecretBox,
                     user_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """
    credential id -> the account it signed in as (an email or platform username), so
    the pages can show which account each connection runs as. People sign in to us
    and to the platforms with company accounts that easily get mixed up.
    """
    found: dict[uuid.UUID, str] = {}
    for credential in session.scalars(select(Credential).where(Credential.user_id == user_id)):
        try:
            found[credential.id] = str(read_secret(box, credential).get("username") or "")
        except DecryptionError:
            found[credential.id] = ""
    return found


class NotLinked(Exception):
    """The person has not linked this kind of account yet."""


def linked_account(db: Database, user_id: uuid.UUID, provider: str) -> uuid.UUID | None:
    with db.session() as session:
        return session.scalars(
            select(Credential.id).where(Credential.user_id == user_id,
                                        Credential.provider == provider)
        ).first()


def linked_microsoft_account(db: Database, user_id: uuid.UUID) -> uuid.UUID | None:
    return linked_account(db, user_id, "entra")


def provider_token(db: Database, box: SecretBox, tokens: Any, provider: str,
                   user_id: uuid.UUID, scope: str,
                   credential_id: uuid.UUID | None = None) -> tuple[uuid.UUID, str]:
    """
    An access token for `scope` as this person from their linked `provider` account (or
    exactly `credential_id`, when the person has several), and the credential it came
    from. A rotated refresh token is stored. Raises NotLinked, or CredentialRejected when
    the link is dead.
    """
    with db.session() as session:
        query = select(Credential).where(Credential.user_id == user_id,
                                         Credential.provider == provider)
        if credential_id is not None:
            query = query.where(Credential.id == credential_id)
        credential = session.scalars(query).first()
        if credential is None:
            raise NotLinked()
        credential_id, version = credential.id, credential.version
        secret = read_secret(box, credential)

    minted = tokens.access_token(credential_id=str(credential_id), version=version,
                                 scope=scope, secret=secret)
    if minted.new_refresh_token:
        with db.session() as session:
            row = session.get(Credential, credential_id)
            if row is not None:
                refresh_secret(session, box, row,
                               {**secret, "refresh_token": minted.new_refresh_token})
    return credential_id, minted.token


def microsoft_token(db: Database, box: SecretBox, entra: EntraTokens, user_id: uuid.UUID,
                    scope: str) -> tuple[uuid.UUID, str]:
    return provider_token(db, box, entra, "entra", user_id, scope)
