"""
Snowflake OAuth, for Snowflake.

Linking: the person signs in on Snowflake's own sign-in page
(`<account>/oauth/authorize`, authorization code + PKCE) for our OAuth security
integration. We keep the refresh token Snowflake returns (encrypted, via
credentials.service). Each call exchanges it for a short-lived access token
(`<account>/oauth/token-request`), cached per copy of the service.

The integration is created once by a Snowflake admin, e.g.:

    CREATE SECURITY INTEGRATION iq_connect
      TYPE = OAUTH  OAUTH_CLIENT = CUSTOM  OAUTH_CLIENT_TYPE = 'CONFIDENTIAL'
      OAUTH_REDIRECT_URI = '<PUBLIC_BASE_URL>/auth/platform/snowflake/callback'
      OAUTH_ISSUE_REFRESH_TOKENS = TRUE  OAUTH_REFRESH_TOKEN_VALIDITY = 7776000
      ENABLED = TRUE;
    SELECT SYSTEM$SHOW_OAUTH_CLIENT_SECRETS('IQ_CONNECT');  -- client id + secret

The client id and secret belong to the integration (the server), set in .env.
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

_EARLY_REFRESH_SECONDS = 120  # Snowflake access tokens live about ten minutes
_MAX_CACHED_TOKENS = 1000


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class SnowflakeOAuth:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._settings = settings
        self.account = settings.snowflake_account
        self.base = settings.snowflake_url
        self._http = http or httpx.Client(timeout=30.0)
        self._cache: OrderedDict[tuple[str, int], tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _token(self, form: dict[str, str]) -> dict[str, Any]:
        response = self._http.post(
            f"{self.base}/oauth/token-request", data=form,
            auth=(self._settings.snowflake_oauth_client_id,
                  self._settings.snowflake_oauth_client_secret.get_secret_value()),
        )
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
            "client_id": self._settings.snowflake_oauth_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "refresh_token",
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        })
        return (f"{self.base}/oauth/authorize?{query}",
                {"verifier": verifier, "state": state, "redirect_uri": redirect_uri})

    def finish_link(self, kept: dict[str, Any], params: dict[str, str]) -> LinkedAccount:
        if not params.get("code") or not secrets.compare_digest(
            params.get("state", ""), kept.get("state", "")
        ):
            raise SignInFailed("The Snowflake sign-in could not be verified. Please try again.")
        body = self._token({
            "grant_type": "authorization_code", "code": params["code"],
            "redirect_uri": kept.get("redirect_uri", ""), "code_verifier": kept.get("verifier", ""),
        })
        if "error" in body or not body.get("access_token"):
            log.warning("Snowflake sign-in failed: %s", body.get("error"))
            raise SignInFailed("Snowflake did not complete the sign-in. Please try again.")
        if not body.get("refresh_token"):
            raise SignInFailed("Snowflake did not grant lasting access. The OAuth integration "
                               "needs OAUTH_ISSUE_REFRESH_TOKENS = TRUE.")
        username = str(body.get("username", ""))
        return LinkedAccount(tenant=self.account, subject=username, username=username,
                             refresh_token=body["refresh_token"])

    # --- per call ---------------------------------------------------------------

    def access_token(self, *, credential_id: str, version: int, refresh_token: str,
                     scope: str = "session") -> AccessToken:
        key = (credential_id, version)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[1] - _EARLY_REFRESH_SECONDS > time.time():
                self._cache.move_to_end(key)
                return AccessToken(cached[0], None)
        try:
            body = self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
        except httpx.HTTPError:
            raise AdapterError("Snowflake sign-in is unavailable right now. Try again.") from None
        if "error" in body or not body.get("access_token"):
            log.warning("Snowflake token exchange failed for %s: %s",
                        credential_id, body.get("error"))
            if body.get("error") in {"invalid_grant", "invalid_token", "invalid_client"}:
                raise CredentialRejected(
                    "Snowflake no longer accepts this sign-in (it expired or was revoked)."
                )
            raise AdapterError("Snowflake sign-in is unavailable right now. Try again.")
        expires = time.time() + float(body.get("expires_in", 600))
        with self._lock:
            self._cache[key] = (body["access_token"], expires)
            while len(self._cache) > _MAX_CACHED_TOKENS:
                self._cache.popitem(last=False)
        new_refresh = body.get("refresh_token")
        return AccessToken(body["access_token"],
                           new_refresh if new_refresh and new_refresh != refresh_token else None)
