"""
How Snowflake is signed in to. Each person brings their own Snowflake account; two methods:

  sso              Their Microsoft account through Snowflake External OAuth (the default).
                   Their Snowflake admin creates an External OAuth security integration that
                   trusts their Entra tenant, and an Entra "resource" app exposing one scope
                   per role (`api://<id>/session:scope:<role>`). IQ Connect asks Microsoft for
                   exactly that scope from the person's Microsoft link, so the token can open
                   only that role. README section 7.4.
  snowflake_oauth  Snowflake's own OAuth (code + PKCE) for a custom OAuth security
                   integration in their account (its client id + secret -- theirs, or this
                   server's default SNOWFLAKE_OAUTH_CLIENT_ID). The person signs in on
                   Snowflake's page (their company SSO if Snowflake is set up for it). With a
                   role, the token is limited to `session:role:<ROLE>`.

For snowflake_oauth the credential's encrypted secret holds the account, the client (or
`server_app: true`), the role and, once signed in, the refresh token. No Snowflake
password or key pair is ever stored or used.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlencode

import httpx

from backend.core.auth.identity import SignInFailed
from backend.core.auth.microsoft import AccessToken, LinkedAccount
from backend.core.configuration.settings import Settings
from backend.core.dynamic_registry.base import AdapterError, CredentialRejected

log = logging.getLogger(__name__)

SSO = "sso"
SNOWFLAKE_OAUTH = "snowflake_oauth"
# What the token is asked for: Snowflake OAuth's own token needs no extra scope.
SNOWFLAKE_SCOPE = "session"
_EARLY_REFRESH_SECONDS = 120  # Snowflake access tokens live about ten minutes
_MAX_CACHED_TOKENS = 1000
_ACCOUNT = re.compile(r"[a-z0-9_.-]+")
_ROLE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}")
_ENTRA_SCOPE = re.compile(r"(api://|https://)[^\s/]+(/[^\s/]+)*/session:scope:([A-Za-z0-9_$]+)")


class SnowflakeRejected(Exception):
    """The account, role or OAuth details were refused. The message is safe to show."""


def account_id(value: str) -> str:
    """A Snowflake account identifier (it becomes part of the URL we call), or refused."""
    value = value.strip().lower().removeprefix("https://").rstrip("/")
    value = value.removesuffix(".snowflakecomputing.com")
    if not value or not _ACCOUNT.fullmatch(value):
        raise SnowflakeRejected("Enter the account identifier, like myorg-myaccount or "
                                "xy12345.eu-west-1 (the part before .snowflakecomputing.com).")
    return value


def account_url(account: str) -> str:
    return f"https://{account}.snowflakecomputing.com"


def role_name(value: str) -> str:
    """A plain role name, upper-cased; "" for none."""
    value = value.strip()
    if value and not _ROLE.fullmatch(value):
        raise SnowflakeRejected("The role is a plain Snowflake role name, like ANALYST.")
    return value.upper()


def entra_scope(value: str) -> tuple[str, str]:
    """
    The Entra scope that opens one Snowflake role, and that role. Any-role scopes are
    refused: they would let a token open every role the person has.
    """
    value = value.strip()
    if "session:role-any" in value or "session:scope-any" in value:
        raise SnowflakeRejected("The scope must name one role (.../session:scope:<role>), "
                                "not any role.")
    found = _ENTRA_SCOPE.fullmatch(value)
    if not found:
        raise SnowflakeRejected("The scope looks like api://<resource app id>/"
                                "session:scope:<role>.")
    return value, found.group(3).upper()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class SnowflakeOAuth:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._settings = settings
        self.http = http or httpx.Client(timeout=30.0)
        self._cache: OrderedDict[tuple[str, int], tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def ready(self) -> bool:
        """Always: every person may bring their own OAuth integration."""
        return True

    @property
    def server_app(self) -> tuple[str, str]:
        settings = self._settings
        return (settings.snowflake_oauth_client_id,
                settings.snowflake_oauth_client_secret.get_secret_value())

    def _client(self, secret: dict[str, Any]) -> tuple[str, str]:
        if secret.get("server_app"):
            return self.server_app
        return str(secret.get("client_id", "")), str(secret.get("client_secret", ""))

    def _base(self, secret: dict[str, Any]) -> str:
        return account_url(str(secret.get("account") or self._settings.snowflake_account))

    def _token(self, secret: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
        response = self.http.post(f"{self._base(secret)}/oauth/token-request", data=form,
                                  auth=self._client(secret))
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200:
            return {"error": body.get("error", f"http_{response.status_code}")}
        return body

    # --- linking (browser round trip) -------------------------------------------

    def begin_link(self, redirect_uri: str, secret: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(16)
        role = str(secret.get("role") or "")
        query = urlencode({
            "client_id": self._client(secret)[0],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            # With a role the token can only ever open that one role.
            "scope": "refresh_token" + (f" session:role:{role}" if role else ""),
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        })
        return (f"{self._base(secret)}/oauth/authorize?{query}",
                {"verifier": verifier, "state": state, "redirect_uri": redirect_uri})

    def finish_link(self, kept: dict[str, Any], params: dict[str, str],
                    secret: dict[str, Any]) -> LinkedAccount:
        if not params.get("code") or not secrets.compare_digest(
            params.get("state", ""), kept.get("state", "")
        ):
            raise SignInFailed("The Snowflake sign-in could not be verified. Please try again.")
        body = self._token(secret, {
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
        return LinkedAccount(tenant=str(secret.get("account", "")), subject=username,
                             username=username, refresh_token=body["refresh_token"])

    # --- per call ---------------------------------------------------------------

    def access_token(self, *, credential_id: str, version: int, scope: str = SNOWFLAKE_SCOPE,
                     refresh_token: str = "", secret: dict[str, Any] | None = None
                     ) -> AccessToken:
        secret = dict(secret or {})
        refresh_token = refresh_token or str(secret.get("refresh_token", ""))
        key = (credential_id, version)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[1] - _EARLY_REFRESH_SECONDS > time.time():
                self._cache.move_to_end(key)
                return AccessToken(cached[0], None)
        if not refresh_token:
            raise CredentialRejected("This Snowflake sign-in was never completed.")
        try:
            body = self._token(secret, {"grant_type": "refresh_token",
                                        "refresh_token": refresh_token})
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
