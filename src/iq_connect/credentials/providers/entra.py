"""
Microsoft Entra delegated access, for Fabric (and later Azure Databricks).

Linking: the person signs in with Microsoft once, consenting to Fabric access.
We keep the refresh token Entra returns (encrypted, via credentials.service) --
never a password.

Using: for each call, the refresh token is exchanged for a short-lived access
token for exactly the resource the call needs (warehouse SQL, Fabric REST,
Power BI). Only that access token reaches the adapter. Access tokens are
cached per copy of the service until shortly before they expire, so a busy
conversation does not ask Entra on every query.

Entra may return a new refresh token on redemption; the caller stores it so
the link keeps working (returned here as `new_refresh_token`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from ...adapters.base import AdapterError, CredentialRejected
from ...adapters.fabric.scopes import FABRIC_API_SCOPE
from ...identity.providers import SignInFailed
from ...settings import Settings

log = logging.getLogger(__name__)


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
    def __init__(self, settings: Settings, app: Any = None) -> None:
        self._settings = settings
        self._app = app  # an msal.ConfidentialClientApplication; tests pass a fake
        self._cache: OrderedDict[tuple[str, int, str], tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _msal(self) -> Any:
        if self._app is None:
            import msal

            self._app = msal.ConfidentialClientApplication(
                self._settings.entra_client_id,
                authority=f"https://login.microsoftonline.com/{self._settings.entra_tenant}",
                client_credential=self._settings.entra_client_secret.get_secret_value(),
            )
        return self._app

    # --- linking (browser round trip) -------------------------------------------

    def begin_link(self, redirect_uri: str, scopes: list[str] | None = None
                   ) -> tuple[str, dict[str, Any]]:
        # The resource asked for here is the one the person consents to now; the
        # refresh token then serves every resource they have consented to.
        flow = self._msal().initiate_auth_code_flow(
            scopes=scopes or [FABRIC_API_SCOPE], redirect_uri=redirect_uri,
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
        if tenant not in self._settings.allowed_tenants:
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

    def access_token(self, *, credential_id: str, version: int, refresh_token: str,
                     scope: str) -> AccessToken:
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
