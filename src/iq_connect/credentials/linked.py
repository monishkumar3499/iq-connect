"""
Using a person's linked platform account outside a tool call: the setup wizard
and the setup command browse Fabric or Databricks as the person with it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from ..db.models import Credential
from ..db.session import Database
from ..security.crypto import SecretBox
from .providers.entra import EntraTokens
from .service import read_secret, refresh_secret


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
                   user_id: uuid.UUID, scope: str) -> tuple[uuid.UUID, str]:
    """
    An access token for `scope` as this person from their linked `provider`
    account, and the credential it came from. A rotated refresh token is
    stored. Raises NotLinked, or CredentialRejected when the link is dead.
    """
    with db.session() as session:
        credential = session.scalars(
            select(Credential).where(Credential.user_id == user_id,
                                     Credential.provider == provider)
        ).first()
        if credential is None:
            raise NotLinked()
        credential_id, version = credential.id, credential.version
        secret = read_secret(box, credential)

    minted = tokens.access_token(credential_id=str(credential_id), version=version,
                                 refresh_token=secret["refresh_token"], scope=scope)
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
