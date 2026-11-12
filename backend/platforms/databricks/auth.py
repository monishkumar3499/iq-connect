"""
How Databricks is signed in to. Each person brings their own workspace; two methods:

  sso               Their Microsoft account (Azure Databricks only, the default): a token
                    for Azure Databricks' own resource from their Microsoft link. Needs the
                    delegated permission AzureDatabricks -> user_impersonation on the
                    Microsoft app.
  databricks_oauth  Databricks' own OAuth (user-to-machine, any cloud): the person signs in
                    on their workspace's page (`<host>/oidc/v1/authorize`, code + PKCE). The
                    OAuth app is an "App connection" in their Databricks account console
                    (redirect URL `<PUBLIC_BASE_URL>/auth/platform/databricks/callback`,
                    scopes `all-apis offline_access`) -- theirs, or this server's default
                    (DATABRICKS_OAUTH_CLIENT_ID).

For databricks_oauth the credential's encrypted secret holds the workspace host, the
OAuth client (or `server_app: true`) and, once signed in, the refresh token. Each call
exchanges the refresh token for a short-lived access token (cached per copy).

Workspace addresses are limited to Databricks' own domains: a person's token is only
ever sent to a real Databricks workspace, never to a host someone typed in.
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
from urllib.parse import urlencode, urlsplit

import httpx

from backend.core.auth.identity import SignInFailed
from backend.core.auth.microsoft import AccessToken, LinkedAccount
from backend.core.configuration.settings import Settings
from backend.core.dynamic_registry.base import AdapterError, CredentialRejected

log = logging.getLogger(__name__)

SSO = "sso"
DATABRICKS_OAUTH = "databricks_oauth"
SCOPES = "all-apis offline_access"
TOKEN_SCOPE = "all-apis"
# Azure Databricks accepts a Microsoft (Entra) token for its own resource id.
AZURE_DATABRICKS_SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/user_impersonation"
AZURE_SUFFIX = ".azuredatabricks.net"
# Every cloud Databricks runs on; nothing else is ever called with a token.
WORKSPACE_SUFFIXES = (AZURE_SUFFIX, ".cloud.databricks.com", ".gcp.databricks.com")
_EARLY_REFRESH_SECONDS = 300
_MAX_CACHED_TOKENS = 1000


class DatabricksRejected(Exception):
    """The workspace or OAuth app was refused. The message is safe to show."""


def workspace_host(value: str, *, azure_only: bool = False) -> str:
    """`https://<host>` of a Databricks workspace, or DatabricksRejected."""
    value = value.strip().rstrip("/")
    if value and "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.query or parts.fragment or parts.port:
        raise DatabricksRejected("Enter the workspace URL, like https://adb-1234567890.12."
                                 "azuredatabricks.net or https://dbc-xxxx.cloud.databricks.com.")
    if azure_only and not host.endswith(AZURE_SUFFIX):
        raise DatabricksRejected("Signing in with Microsoft works for Azure Databricks "
                                 "(…azuredatabricks.net). For other clouds choose Databricks "
                                 "sign-in.")
    if not host.endswith(WORKSPACE_SUFFIXES):
        raise DatabricksRejected("That isn't a Databricks workspace address (it should end in "
                                 "azuredatabricks.net, cloud.databricks.com or "
                                 "gcp.databricks.com).")
    return f"https://{host}"


def check_workspace(http: httpx.Client, host: str, client_id: str = "",
                    client_secret: str = "", redirect_uri: str = "") -> None:
    """Is `host` a Databricks workspace, and (when given) does it know the OAuth client?"""
    try:
        meta = http.get(f"{host}/oidc/.well-known/oauth-authorization-server")
        token_url = meta.json().get("token_endpoint") if meta.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        token_url = None
    if not token_url:
        raise DatabricksRejected("That doesn't look like a Databricks workspace (its sign-in "
                                 "information isn't there). Check the workspace URL.")
    if not client_id:
        return
    # A made-up code: Databricks answers invalid_client only when the client is wrong.
    form = {"grant_type": "authorization_code", "code": "iq-connect-check",
            "redirect_uri": redirect_uri, "client_id": client_id}
    if client_secret:
        form["client_secret"] = client_secret
    try:
        answer = http.post(token_url, data=form)
        error = answer.json().get("error", "") if answer.status_code != 200 else ""
    except (httpx.HTTPError, ValueError):
        raise DatabricksRejected("Couldn't reach Databricks to check the app. Try again.") from None
    if error == "invalid_client":
        raise DatabricksRejected("Databricks doesn't accept that client ID and secret. Check "
                                 "the App connection in the Databricks account console.")


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class DatabricksOAuth:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._settings = settings
        self.http = http or httpx.Client(timeout=30.0)
        self._cache: OrderedDict[tuple[str, int], tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def ready(self) -> bool:
        """Always: every person may bring their own OAuth app."""
        return True

    @property
    def server_app(self) -> tuple[str, str]:
        """This server's default OAuth app (client id, secret); ("", "") when none."""
        settings = self._settings
        return (settings.databricks_oauth_client_id,
                settings.databricks_oauth_client_secret.get_secret_value())

    def _client(self, secret: dict[str, Any]) -> dict[str, str]:
        if secret.get("server_app"):
            client_id, client_secret = self.server_app
        else:
            client_id = str(secret.get("client_id", ""))
            client_secret = str(secret.get("client_secret", ""))
        return {"client_id": client_id, **({"client_secret": client_secret}
                                           if client_secret else {})}

    def _host(self, secret: dict[str, Any]) -> str:
        return str(secret.get("host") or self._settings.databricks_host).rstrip("/")

    def _token(self, secret: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
        response = self.http.post(f"{self._host(secret)}/oidc/v1/token",
                                  data={**form, **self._client(secret)})
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
        query = urlencode({
            "client_id": self._client(secret)["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        })
        return (f"{self._host(secret)}/oidc/v1/authorize?{query}",
                {"verifier": verifier, "state": state, "redirect_uri": redirect_uri})

    def finish_link(self, kept: dict[str, Any], params: dict[str, str],
                    secret: dict[str, Any]) -> LinkedAccount:
        if not params.get("code") or not secrets.compare_digest(
            params.get("state", ""), kept.get("state", "")
        ):
            raise SignInFailed("The Databricks sign-in could not be verified. Please try again.")
        body = self._token(secret, {
            "grant_type": "authorization_code", "code": params["code"],
            "redirect_uri": kept.get("redirect_uri", ""), "code_verifier": kept.get("verifier", ""),
        })
        if "error" in body or not body.get("access_token"):
            log.warning("Databricks sign-in failed: %s", body.get("error"))
            raise SignInFailed("Databricks did not complete the sign-in. Please try again.")
        if not body.get("refresh_token"):
            raise SignInFailed("Databricks did not grant lasting access (offline_access).")
        username = self._who(secret, body["access_token"])
        return LinkedAccount(tenant=self._host(secret), subject=username, username=username,
                             refresh_token=body["refresh_token"])

    def _who(self, secret: dict[str, Any], access_token: str) -> str:
        response = self.http.get(f"{self._host(secret)}/api/2.0/preview/scim/v2/Me",
                                 headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code != 200:
            raise SignInFailed("Databricks accepted the sign-in but would not say who you are.")
        return str(response.json().get("userName", ""))

    # --- per call ---------------------------------------------------------------

    def access_token(self, *, credential_id: str, version: int, scope: str = TOKEN_SCOPE,
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
            raise CredentialRejected("This Databricks sign-in was never completed.")
        try:
            body = self._token(secret, {"grant_type": "refresh_token",
                                        "refresh_token": refresh_token})
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
