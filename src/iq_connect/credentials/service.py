"""
Storing and reading people's platform logins.

A secret is a small JSON object (e.g. {"refresh_token": "..."} or
{"private_key": "..."}), encrypted as a whole. Nothing outside this module
touches Credential.ciphertext directly.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Credential, User
from ..security.crypto import Sealed, SecretBox


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
