"""
The server's Microsoft (Entra) app registration -- from .env, or entered once in the setup
UI, validated, stored encrypted. It signs people in to IQ Connect (any company's Microsoft
account: the app is multi-tenant) and grants Fabric, Azure Databricks and Snowflake
External OAuth access as them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from backend.core.configuration.settings import Settings
from backend.core.security.crypto import DecryptionError, Sealed, SecretBox
from backend.shared.database import Database, utcnow
from backend.shared.models import AppRegistration

MAX_SECRET = 512


class AppRejected(Exception):
    """The values were refused (format, or by the platform). The message is safe to show."""


def setup_code(signing_key: bytes) -> str:
    """
    The operator's setup code: derived from the signing key every copy shares, so
    it is the same on every copy and changes only if that key is rotated.
    """
    digest = hmac.new(signing_key, b"iq-connect-microsoft-setup", hashlib.sha256).digest()
    raw = base64.b32encode(digest).decode()[:12]
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def code_matches(expected: str, given: str) -> bool:
    def normal(value: str) -> str:
        return re.sub(r"[\s-]", "", value).upper()

    return bool(given) and hmac.compare_digest(normal(expected), normal(given))


def check_secret_shape(secret: str, *, required: bool = True) -> str:
    if not secret.strip():
        if required:
            raise AppRejected("Enter the client secret's value.")
        return ""
    if len(secret) > MAX_SECRET or any(c in secret for c in "\r\n\t"):
        raise AppRejected("That doesn't look like a client secret. Copy its value again.")
    return secret.strip()


def context(provider: str, client_id: str) -> dict[str, str]:
    # Binds the secret to the app it belongs to: a ciphertext copied onto another
    # provider or client id does not decrypt.
    return {"table": "app_registrations", "provider": provider, "client_id": client_id}


def row_for(session: Any, provider: str) -> AppRegistration | None:
    return session.scalars(select(AppRegistration).where(
        AppRegistration.provider == provider)).one_or_none()


def seal_into(row: AppRegistration, box: SecretBox, provider: str, client_id: str,
              secret: str) -> None:
    sealed = box.seal(json.dumps({"client_secret": secret}).encode(), context(provider, client_id))
    row.ciphertext, row.wrapped_key = sealed.ciphertext, sealed.wrapped_key
    row.key_version = sealed.key_version


def open_from(row: AppRegistration, box: SecretBox, provider: str) -> str:
    """The stored secret. Raises DecryptionError if it doesn't belong to this row."""
    sealed = Sealed(row.ciphertext, row.wrapped_key, row.key_version)
    try:
        plain = box.open(sealed, context(provider, row.client_id))
    except DecryptionError:
        if row.tenant_id is None:
            raise
        # Saved before the tenant became optional: it was bound to the tenant too.
        plain = box.open(sealed, {**context(provider, row.client_id), "tenant": row.tenant_id})
    return json.loads(plain)["client_secret"]


def may_change(*, is_admin: bool, code_ok: bool, claimed: bool, code_required: bool) -> bool:
    """
    Who may enter or replace an app registration:
      - an IQ admin (signed in) or whoever has the setup code, always;
      - anyone, while nothing is claimed yet, where the setup code isn't required
        (development); a claimed app can only be changed by an admin or the code.
    """
    return is_admin or code_ok or (not claimed and not code_required)


log = logging.getLogger(__name__)

MICROSOFT_PROVIDER = "entra"
ANY_TENANT = "organizations"
_GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
# A token for Microsoft Graph with no permissions at all: enough to prove that the
# tenant, the app and the secret belong together, and it grants nothing.
_CHECK_SCOPE = "https://graph.microsoft.com/.default"

# Microsoft's error codes, in words a person can act on.
_AADSTS = {
    "7000215": "Microsoft says the client secret is wrong. Copy the secret's Value "
               "(not its Secret ID) from Certificates & secrets.",
    "7000222": "That client secret has expired. Create a new one in Certificates & secrets.",
    "700016": "Microsoft can't find an app with that Application (client) ID.",
    "90002": "Microsoft can't find that Directory (tenant) ID.",
    "900023": "That Directory (tenant) ID isn't valid.",
    "50194": "Your app registration is single-tenant. In Azure set Supported account types "
             "to 'Accounts in any organizational directory', or enter your Directory "
             "(tenant) ID in the IQ Connect Microsoft setup.",
    "50011": "The redirect URI isn't registered on the app. Add the addresses shown on the "
             "IQ Connect Microsoft setup page under Authentication.",
    "65001": "Admin consent is missing for this app. Grant it under API permissions.",
}


def explain_aadsts(text: str) -> str | None:
    """A plain sentence for a Microsoft error description, or None if unknown."""
    code = next((c for c in _AADSTS if f"AADSTS{c}" in (text or "")), None)
    return _AADSTS[code] if code else None


@dataclass(frozen=True)
class EntraApp:
    tenant: str  # "organizations" (any company), or one tenant GUID
    client_id: str
    client_secret: str = field(repr=False)
    source: str = "settings"  # settings | database
    version: int = 0
    # None: every company's people may sign in (multi-tenant).
    allowed_tenants: frozenset[str] | None = None

    @property
    def key(self) -> tuple[str, int, str, str]:
        """Changes whenever a sign-in client built from these values must be rebuilt."""
        return (self.source, self.version, self.tenant, self.client_id)


@dataclass(frozen=True)
class AppStatus:
    """What may be shown about the app: never the secret."""

    source: str
    tenant: str  # "" for any company
    client_id: str
    updated_at: datetime | None = None
    validated_at: datetime | None = None

    @property
    def claimed(self) -> bool:
        """Checked with Microsoft, or someone has signed in with it: changes need an admin."""
        return self.source == "settings" or self.validated_at is not None


def check_with_microsoft(tenant: str, client_id: str, client_secret: str) -> None:
    """Ask Microsoft for an app-only token with these values. Raises AppRejected."""
    import msal

    try:
        app = msal.ConfidentialClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{tenant}",
            client_credential=client_secret)
        result = app.acquire_token_for_client(scopes=[_CHECK_SCOPE])
    except ValueError as error:  # MSAL refuses an authority it can't discover
        log.info("Microsoft app check could not start: %s", type(error).__name__)
        raise AppRejected("Microsoft can't find that Directory (tenant) ID.") from None
    except Exception as error:  # network trouble: say so, don't guess
        log.warning("Microsoft app check failed to reach Microsoft: %s", type(error).__name__)
        raise AppRejected("Couldn't reach Microsoft to check the values. Try again.") from None
    if "access_token" in result:
        return
    log.info("Microsoft refused the app check: %s", result.get("error", "unknown"))
    raise AppRejected(explain_aadsts(str(result.get("error_description", ""))) or
                      "Microsoft refused these values (" + str(result.get("error", "error"))
                      + "). Check the client ID and secret.")


class MicrosoftApps:
    def __init__(self, settings: Settings, db: Database, box: SecretBox, *,
                 ttl: float = 15.0,
                 check: Callable[[str, str, str], None] = check_with_microsoft) -> None:
        self._settings = settings
        self._db = db
        self._box = box
        self._ttl = ttl
        self.check = check  # replaceable: tests stand in for Microsoft
        self._lock = threading.Lock()
        self._cached: tuple[float, EntraApp | None] | None = None

    # --- reading -----------------------------------------------------------------

    def current(self) -> EntraApp | None:
        settings = self._settings
        if settings.entra_configured:
            return EntraApp(settings.entra_tenant, settings.entra_client_id,
                            settings.entra_client_secret.get_secret_value(), "settings", 0,
                            settings.allowed_tenants)
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < self._ttl:
                return self._cached[1]
        found = self._load()
        with self._lock:
            self._cached = (time.monotonic(), found)
        return found

    def ready(self) -> bool:
        return self.current() is not None

    def _load(self) -> EntraApp | None:
        with self._db.session() as session:
            row = row_for(session, MICROSOFT_PROVIDER)
            if row is None:
                return None
            try:
                secret = open_from(row, self._box, MICROSOFT_PROVIDER)
            except DecryptionError:
                log.error("The stored Microsoft app registration can't be decrypted; "
                          "enter it again in the setup UI")
                return None
            tenant = row.tenant_id or ""
            allowed = frozenset({tenant}) if tenant else self._settings.allowed_tenants
            return EntraApp(tenant or ANY_TENANT, row.client_id, secret, "database",
                            row.version, allowed)

    def status(self) -> AppStatus | None:
        if self._settings.entra_configured:
            return AppStatus("settings", self._settings.entra_tenant,
                             self._settings.entra_client_id)
        with self._db.session() as session:
            row = row_for(session, MICROSOFT_PROVIDER)
            if row is None:
                return None
            return AppStatus("database", row.tenant_id or "", row.client_id, row.updated_at,
                             row.validated_at)

    def forget(self) -> None:
        with self._lock:
            self._cached = None

    # --- saving ------------------------------------------------------------------

    @staticmethod
    def validate_format(tenant: str, client_id: str,
                        client_secret: str) -> tuple[str, str, str]:
        tenant, client_id = tenant.strip().lower(), client_id.strip().lower()
        if tenant and not _GUID.match(tenant):
            raise AppRejected("The Directory (tenant) ID is a GUID like "
                              "11111111-2222-3333-4444-555555555555, or leave it empty.")
        if not _GUID.match(client_id):
            raise AppRejected("The Application (client) ID is a GUID like "
                              "11111111-2222-3333-4444-555555555555 (Overview page of the app).")
        return tenant, client_id, check_secret_shape(client_secret)

    def save(self, *, client_id: str, client_secret: str, tenant: str = "",
             user_id: uuid.UUID | None) -> EntraApp:
        """Check the values (with Microsoft when a tenant is given), then store them."""
        if self._settings.entra_configured:
            raise AppRejected("This server's Microsoft app is set in its settings file (.env) "
                              "and can only be changed there.")
        tenant, client_id, client_secret = self.validate_format(tenant, client_id, client_secret)
        if tenant:
            self.check(tenant, client_id, client_secret)
        with self._db.session() as session:
            row = row_for(session, MICROSOFT_PROVIDER)
            if row is None:
                row = AppRegistration(provider=MICROSOFT_PROVIDER, version=0)
                session.add(row)
            row.tenant_id, row.client_id, row.account = tenant or None, client_id, None
            seal_into(row, self._box, MICROSOFT_PROVIDER, client_id, client_secret)
            row.version = (row.version or 0) + 1
            row.validated_at = utcnow() if tenant else None
            row.updated_by = user_id
        self.forget()
        log.info("Microsoft app registration saved (client %s, tenant %s)", client_id,
                 tenant or "any company")
        found = self.current()
        assert found is not None
        return found

    def mark_used(self) -> None:
        """
        The first successful sign-in proves an app entered without a tenant: from then on
        only an admin (or the setup code) may replace it. One conditional UPDATE.
        """
        if self._settings.entra_configured:
            return
        with self._db.session() as session:
            changed = session.execute(
                update(AppRegistration)
                .where(AppRegistration.provider == MICROSOFT_PROVIDER,
                       AppRegistration.validated_at.is_(None))
                .values(validated_at=utcnow())
            ).rowcount
        if changed:
            self.forget()
