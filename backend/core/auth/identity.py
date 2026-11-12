"""
Identity providers: who is this person? Answered by Microsoft (Entra) in real
use, or by a type-any-email form while developing.

Both follow the same two steps, so the routes don't care which is active:
  begin(redirect_uri, scopes) -> where to send the browser, plus state to keep
  finish(kept, query_params)  -> the verified Identity

`scopes` lets one Microsoft sign-in also grant a platform's access (Fabric, or
Azure Databricks): the person picks their account once, and the refresh token
that comes back is stored as their platform link.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

from itsdangerous import BadSignature, URLSafeTimedSerializer

from backend.core.configuration.settings import Settings

log = logging.getLogger(__name__)

DEV_ISSUER = "dev://local"


@dataclass(frozen=True)
class Identity:
    issuer: str  # who vouches: one per company (Entra tenant)
    subject: str  # the person's stable id at that issuer (Entra "oid")
    tenant: str
    email: str
    name: str
    # Only when platform access was asked for at sign-in; stored encrypted, never shown.
    refresh_token: str = field(default="", repr=False)


class SignInFailed(Exception):
    """The identity provider did not confirm who this is. Message is safe to show."""


class IdentityProvider(Protocol):
    name: str

    def begin(self, redirect_uri: str, scopes: list[str] | None = None
              ) -> tuple[str, dict[str, Any]]: ...

    def finish(self, kept: dict[str, Any], params: dict[str, str]) -> Identity: ...


# ---------------------------------------------------------------------------
# Microsoft Entra ID
# ---------------------------------------------------------------------------


def identity_from_entra_claims(claims: dict[str, Any],
                               allowed_tenants: frozenset[str] | None) -> Identity:
    """
    Turn a validated Entra ID token into an Identity, refusing other companies.
    `allowed_tenants` None: the app isn't pinned yet, and this sign-in will pin it.
    """
    tenant = str(claims.get("tid", "")).lower()
    subject = str(claims.get("oid", ""))
    if not tenant or not subject:
        raise SignInFailed("Microsoft did not return an organisation account.")
    if allowed_tenants is not None and tenant not in allowed_tenants:
        log.warning("Sign-in refused for tenant %s (not allowed)", tenant)
        raise SignInFailed("Your organisation is not set up to use this service.")
    email = claims.get("preferred_username") or claims.get("email") or claims.get("upn") or ""
    return Identity(
        # Fixed per tenant, so the same company always maps to the same org.
        issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
        subject=subject,
        tenant=tenant,
        email=str(email),
        name=str(claims.get("name", "")),
    )


class EntraProvider:
    """
    OpenID Connect via MSAL: authorization code flow with PKCE and a nonce. MSAL
    checks the ID token's signature, issuer, audience, nonce and expiry.
    """

    name = "entra"

    def __init__(self, settings: Settings, apps: Any = None) -> None:
        from backend.core.auth.microsoft import MsalClient

        # The app registration may come from the setup UI, so it is looked up
        # each time; MSAL itself is built on first use (it contacts Microsoft).
        self.client = MsalClient(settings, apps)

    @property
    def _app(self) -> Any:
        return self.client._app

    @_app.setter
    def _app(self, fake: Any) -> None:
        self.client._app, self.client._built_for = fake, None

    def ready(self) -> bool:
        return self.client.ready()

    def _msal(self) -> Any:
        from backend.core.auth.microsoft import NotConfigured

        try:
            return self.client.get()
        except NotConfigured:
            raise SignInFailed("Microsoft sign-in isn't set up on this server yet.") from None

    def begin(self, redirect_uri: str, scopes: list[str] | None = None
              ) -> tuple[str, dict[str, Any]]:
        # Without scopes this sign-in only identifies the person. With a platform's
        # scope it also asks for that access, so no second Microsoft trip is needed.
        flow = self._msal().initiate_auth_code_flow(
            scopes=list(scopes or []), redirect_uri=redirect_uri, prompt="select_account"
        )
        return flow["auth_uri"], {"msal_flow": flow}

    def finish(self, kept: dict[str, Any], params: dict[str, str]) -> Identity:
        result = self._msal().acquire_token_by_auth_code_flow(kept.get("msal_flow", {}), params)
        if "error" in result:
            from backend.core.configuration.app_registrations import explain_aadsts

            log.warning("Entra sign-in failed: %s", result.get("error"))
            raise SignInFailed(explain_aadsts(str(result.get("error_description", "")))
                               or "Microsoft sign-in did not complete. Please try again.")
        identity = identity_from_entra_claims(
            result.get("id_token_claims", {}), self.client.allowed_tenants()
        )
        refresh_token = str(result.get("refresh_token") or "")
        if not refresh_token:
            return identity
        return Identity(identity.issuer, identity.subject, identity.tenant, identity.email,
                        identity.name, refresh_token=refresh_token)


# ---------------------------------------------------------------------------
# Development stand-in
# ---------------------------------------------------------------------------


class DevProvider:
    """
    Type any email to be that person. Exists so the whole flow can be built and
    tested before an Entra app registration exists. Settings refuse it outside
    APP_ENV=dev, and its routes do not exist unless it is the active provider.

    The "code" it hands back is the typed identity, signed, so the callback
    can't be fed an identity that did not come from its own form.
    """

    name = "dev"

    def __init__(self, keys: list[bytes]) -> None:
        self._signer = URLSafeTimedSerializer(keys, salt="iq-dev-sign-in")

    def begin(self, redirect_uri: str, scopes: list[str] | None = None
              ) -> tuple[str, dict[str, Any]]:
        return "/auth/dev-login?" + urlencode({"redirect_uri": redirect_uri}), {}

    def make_code(self, email: str, name: str) -> str:
        return self._signer.dumps({"email": email, "name": name})

    def finish(self, kept: dict[str, Any], params: dict[str, str]) -> Identity:
        try:
            data = self._signer.loads(params.get("code", ""), max_age=300)
        except BadSignature:
            raise SignInFailed("The development sign-in could not be verified.") from None
        email = data["email"].strip().lower()
        return Identity(issuer=DEV_ISSUER, subject=f"dev:{email}", tenant="dev",
                        email=email, name=data.get("name") or email.split("@")[0])


def make_identity_provider(settings: Settings, keys: list[bytes],
                           apps: Any = None) -> IdentityProvider:
    if settings.sso_provider == "entra":
        return EntraProvider(settings, apps)
    return DevProvider(keys)
