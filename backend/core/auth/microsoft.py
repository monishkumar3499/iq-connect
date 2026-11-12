"""
Microsoft (Entra) tokens: one MSAL client per app version, linking and refreshing as the
person. Shared by our own sign-in and by every platform that accepts a Microsoft token
(Fabric, Azure Databricks, Snowflake External OAuth).

Every token provider (this one, Databricks OAuth, Snowflake OAuth, Fabric service
principals) answers `access_token(credential_id=, version=, scope=, secret=)` with an
AccessToken, so the pipeline treats them all alike.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from backend.core.auth.identity import SignInFailed
from backend.core.configuration.app_registrations import EntraApp, MicrosoftApps
from backend.core.configuration.settings import Settings
from backend.core.dynamic_registry.base import AdapterError, CredentialRejected


class NotConfigured(Exception):
    """No Microsoft app registration yet (neither .env nor the setup UI)."""


def build_msal(app: EntraApp) -> Any:
    import msal

    return msal.ConfidentialClientApplication(
        app.client_id, authority=f"https://login.microsoftonline.com/{app.tenant}",
        client_credential=app.client_secret)


class MsalClient:
    def __init__(self, settings: Settings, apps: MicrosoftApps | None = None) -> None:
        self._settings = settings
        self._apps = apps
        self.factory = build_msal   # tests replace it
        self._app: Any = None       # tests may set a fake client here directly
        self._built_for: tuple | None = None

    def current(self) -> EntraApp | None:
        if self._apps is not None:
            return self._apps.current()
        settings = self._settings
        if not settings.entra_configured:
            return None
        return EntraApp(settings.entra_tenant, settings.entra_client_id,
                        settings.entra_client_secret.get_secret_value(), "settings", 0,
                        settings.allowed_tenants)

    def ready(self) -> bool:
        return self.current() is not None

    def allowed_tenants(self) -> frozenset[str] | None:
        """Who may sign in. None: not pinned yet -- the first sign-in pins its tenant."""
        app = self.current()
        return app.allowed_tenants if app else frozenset()

    def get(self) -> Any:
        app = self.current()
        if app is None:
            raise NotConfigured()
        if self._app is not None and self._built_for is None:
            return self._app  # injected by a test
        if self._built_for != app.key:
            # Built on first use: MSAL contacts Microsoft while starting.
            self._app, self._built_for = self.factory(app), app.key
        return self._app


log = logging.getLogger(__name__)


# The Fabric API: what a link asks for when the caller names nothing else.
_FABRIC_API_SCOPE = "https://api.fabric.microsoft.com/.default"

# MSAL errors that mean "this link is dead, the person must sign in again".
_RELINK_ERRORS = {"invalid_grant", "interaction_required", "consent_required"}
_EARLY_REFRESH_SECONDS = 300
_MAX_CACHED_TOKENS = 1000


@dataclass(frozen=True)
class LinkedAccount:
    tenant: str
    subject: str  # Entra object id of the Microsoft account that consented
    username: str
    refresh_token: str


@dataclass(frozen=True)
class AccessToken:
    token: str
    new_refresh_token: str | None


class EntraTokens:
    def __init__(self, settings: Settings, apps: MicrosoftApps | None = None,
                 app: Any = None) -> None:
        # The app registration is looked up when needed: it may be entered in the
        # setup UI while the service runs. `app`: a fake MSAL client (tests).
        self.client = MsalClient(settings, apps)
        if app is not None:
            self._app = app
        self._cache: OrderedDict[tuple[str, int, str], tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def _app(self) -> Any:
        return self.client._app

    @_app.setter
    def _app(self, fake: Any) -> None:
        # Tests stand in for Microsoft by setting a fake MSAL client here.
        self.client._app, self.client._built_for = fake, None

    def ready(self) -> bool:
        return self.client.ready()

    def _msal(self) -> Any:
        try:
            return self.client.get()
        except NotConfigured:
            raise AdapterError("Microsoft sign-in isn't set up on this server yet.") from None

    # --- linking (browser round trip) -------------------------------------------

    def begin_link(self, redirect_uri: str, scopes: list[str] | None = None
                   ) -> tuple[str, dict[str, Any]]:
        # The resource asked for here is the one the person consents to now; the
        # refresh token then serves every resource they have consented to.
        flow = self._msal().initiate_auth_code_flow(
            scopes=scopes or [_FABRIC_API_SCOPE], redirect_uri=redirect_uri,
            prompt="select_account",
        )
        return flow["auth_uri"], {"msal_flow": flow}

    def finish_link(self, kept: dict[str, Any], params: dict[str, str]) -> LinkedAccount:
        result = self._msal().acquire_token_by_auth_code_flow(kept.get("msal_flow", {}), params)
        if "error" in result:
            log.warning("Linking Microsoft account failed: %s", result.get("error"))
            raise SignInFailed("Microsoft did not complete the sign-in. Please try again.")
        claims = result.get("id_token_claims") or {}
        tenant = str(claims.get("tid", "")).lower()
        allowed = self.client.allowed_tenants()
        if allowed is not None and tenant not in allowed:
            raise SignInFailed("That Microsoft account's organisation is not allowed here.")
        refresh_token = result.get("refresh_token")
        if not refresh_token:
            raise SignInFailed("Microsoft did not grant lasting access. Please try again.")
        return LinkedAccount(
            tenant=tenant,
            subject=str(claims.get("oid", "")),
            username=str(claims.get("preferred_username", "")),
            refresh_token=refresh_token,
        )

    # --- per call ---------------------------------------------------------------

    def access_token(self, *, credential_id: str, version: int, scope: str,
                     refresh_token: str = "", secret: dict[str, Any] | None = None
                     ) -> AccessToken:
        refresh_token = refresh_token or str((secret or {}).get("refresh_token", ""))
        key = (credential_id, version, scope)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[1] - _EARLY_REFRESH_SECONDS > time.time():
                self._cache.move_to_end(key)
                return AccessToken(cached[0], None)

        result = self._msal().acquire_token_by_refresh_token(refresh_token, scopes=[scope])
        if not isinstance(result, dict) or "access_token" not in result:
            error = (result or {}).get("error", "unknown_error")
            log.warning("Entra token exchange failed for credential %s: %s", credential_id, error)
            if error in _RELINK_ERRORS:
                raise CredentialRejected(
                    "Microsoft no longer accepts this account's sign-in (it expired, was "
                    "revoked, or the password changed)."
                )
            raise AdapterError("Microsoft sign-in is unavailable right now. Try again shortly.")

        expires = time.time() + float(result.get("expires_in", 3600))
        with self._lock:
            self._cache[key] = (result["access_token"], expires)
            while len(self._cache) > _MAX_CACHED_TOKENS:
                self._cache.popitem(last=False)
        new_refresh = result.get("refresh_token")
        return AccessToken(
            result["access_token"], new_refresh if new_refresh != refresh_token else None
        )
