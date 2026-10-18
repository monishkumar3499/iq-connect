"""
Databricks OAuth (user-to-machine), for Databricks on any cloud.

Linking: the person signs in on their workspace's own sign-in page
(`<host>/oidc/v1/authorize`, authorization code + PKCE). We keep the refresh
token Databricks returns (encrypted, via credentials.service).

Using: for each call the refresh token is exchanged for a short-lived access
token (`<host>/oidc/v1/token`). Access tokens are cached per copy of the
service until shortly before they expire. Databricks may return a new refresh
token on redemption; the caller stores it.

Needs an OAuth app connection in the Databricks account console with redirect
URI `<PUBLIC_BASE_URL>/auth/platform/databricks/callback` and scopes
`all-apis offline_access` (DATABRICKS_OAUTH_CLIENT_ID, optional secret).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlencode

import httpx

from ...adapters.base import AdapterError, CredentialRejected
from ...identity.providers import SignInFailed
from ...settings import Settings
from .entra import AccessToken, LinkedAccount

log = logging.getLogger(__name__)

SCOPES = "all-apis offline_access"
_EARLY_REFRESH_SECONDS = 300
_MAX_CACHED_TOKENS = 1000


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class DatabricksOAuth:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._settings = settings
        self.host = settings.databricks_host
        self._http = http or httpx.Client(timeout=30.0)
        self._cache: OrderedDict[tuple[str, int], tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _client_fields(self) -> dict[str, str]:
        fields = {"client_id": self._settings.databricks_oauth_client_id}
        secret = self._settings.databricks_oauth_client_secret.get_secret_value()
        if secret:
            fields["client_secret"] = secret
        return fields

    def _token(self, form: dict[str, str]) -> dict[str, Any]:
        response = self._http.post(f"{self.host}/oidc/v1/token",
                                   data={**form, **self._client_fields()})
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200:
            return {"error": body.get("error", f"http_{response.status_code}")}
        return body

    # --- linking (browser round trip) -------------------------------------------

    def begin_link(self, redirect_uri: str) -> tuple[str, dict[str, Any]]:
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(16)
        query = urlencode({
            "client_id": self._settings.databricks_oauth_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        })
        return (f"{self.host}/oidc/v1/authorize?{query}",
                {"verifier": verifier, "state": state, "redirect_uri": redirect_uri})

    def finish_link(self, kept: dict[str, Any], params: dict[str, str]) -> LinkedAccount:
        if not params.get("code") or not secrets.compare_digest(
            params.get("state", ""), kept.get("state", "")
        ):
            raise SignInFailed("The Databricks sign-in could not be verified. Please try again.")
        body = self._token({
            "grant_type": "authorization_code", "code": params["code"],
            "redirect_uri": kept.get("redirect_uri", ""), "code_verifier": kept.get("verifier", ""),
        })
        if "error" in body or not body.get("access_token"):
            log.warning("Databricks sign-in failed: %s", body.get("error"))
            raise SignInFailed("Databricks did not complete the sign-in. Please try again.")
        if not body.get("refresh_token"):
            raise SignInFailed("Databricks did not grant lasting access (offline_access).")
        username = self._who(body["access_token"])
        return LinkedAccount(tenant=self.host, subject=username, username=username,
                             refresh_token=body["refresh_token"])

    def _who(self, access_token: str) -> str:
        response = self._http.get(f"{self.host}/api/2.0/preview/scim/v2/Me",
                                  headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code != 200:
            raise SignInFailed("Databricks accepted the sign-in but would not say who you are.")
        return str(response.json().get("userName", ""))

    # --- per call ---------------------------------------------------------------

    def access_token(self, *, credential_id: str, version: int, refresh_token: str,
                     scope: str = "all-apis") -> AccessToken:
        key = (credential_id, version)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[1] - _EARLY_REFRESH_SECONDS > time.time():
                self._cache.move_to_end(key)
                return AccessToken(cached[0], None)

        try:
            body = self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
        except httpx.HTTPError:
            raise AdapterError("Databricks sign-in is unavailable right now. Try again.") from None
        if "error" in body or not body.get("access_token"):
            log.warning("Databricks token exchange failed for %s: %s",
                        credential_id, body.get("error"))
            if body.get("error") in {"invalid_grant", "invalid_token", "invalid_client"}:
                raise CredentialRejected(
                    "Databricks no longer accepts this sign-in (it expired or was revoked)."
                )
            raise AdapterError("Databricks sign-in is unavailable right now. Try again.")

        expires = time.time() + float(body.get("expires_in", 3600))
        with self._lock:
            self._cache[key] = (body["access_token"], expires)
            while len(self._cache) > _MAX_CACHED_TOKENS:
                self._cache.popitem(last=False)
        new_refresh = body.get("refresh_token")
        return AccessToken(body["access_token"],
                           new_refresh if new_refresh and new_refresh != refresh_token else None)
