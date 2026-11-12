"""Turning a verified identity into our org and user rows."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.auth.identity import Identity
from backend.shared.database import utcnow
from backend.shared.models import Org, User


def sign_in(session: Session, identity: Identity) -> User:
    """
    Find or create the person. The org is keyed by the identity issuer (one per
    Entra tenant), the person by the issuer's stable subject id -- never by
    email, which can change or be reassigned to someone else.

    Which tenants may sign in at all is decided before this point (settings,
    `allowed_tenants`), so creating an org here never admits a new company.
    """
    org = session.scalars(select(Org).where(Org.idp_issuer == identity.issuer)).one_or_none()
    if org is None:
        domain = identity.email.split("@")[-1] if "@" in identity.email else identity.tenant
        org = Org(name=domain, idp_issuer=identity.issuer)
        session.add(org)
        session.flush()

    user = session.scalars(
        select(User).where(User.org_id == org.id, User.idp_subject == identity.subject)
    ).one_or_none()
    if user is None:
        user = User(org_id=org.id, idp_subject=identity.subject, email=identity.email)
        session.add(user)
    user.email = identity.email or user.email
    user.display_name = identity.name or user.display_name
    user.last_login_at = utcnow()
    session.flush()
    return user


# Sent with every tool answer, so it costs context on every call: keep it bounded.
MAX_INSTRUCTIONS = 4000


def set_instructions(session: Session, user_id, text: str) -> bool:
    """Save the person's instructions for Claude. False (nothing saved) when too long."""
    text = text.replace("\r\n", "\n").strip()
    if len(text) > MAX_INSTRUCTIONS:
        return False
    user = session.get(User, user_id)
    if user is None:
        return False
    user.instructions = text
    return True
