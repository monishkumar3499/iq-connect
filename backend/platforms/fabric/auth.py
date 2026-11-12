"""
How Fabric is signed in to, and the Microsoft resources (token scopes) it needs.

Two ways, chosen by the person in the setup's Sign in step:

  sso                Their own Microsoft account (Entra delegated access, the default):
                     queries run as them, so Fabric's own permissions, row-level security
                     and masking apply to them. The token comes from their Microsoft link
                     (core/auth/microsoft.py).
  service_principal  An Entra app of their organisation (tenant id, client id, client
                     secret), signed in with the client-credentials grant. Queries run as
                     that service principal: it must be added to the Fabric workspaces, and
                     the tenant setting "Service principals can use Fabric APIs" must be on.
                     The secret is stored encrypted on the person's credential and only
                     their own connections use it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any

import httpx

from backend.core.auth.microsoft import AccessToken
from backend.core.dynamic_registry.base import AdapterError, CredentialRejected

log = logging.getLogger(__name__)

FABRIC_API_SCOPE = "https://api.fabric.microsoft.com/.default"
# The double slash is intentional: it is the documented form for the SQL
# resource "https://database.windows.net/" plus "/.default".
SQL_SCOPE = "https://database.windows.net//.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

SSO = "sso"
SERVICE_PRINCIPAL = "service_principal"
# The Credential.provider that holds a service principal's tenant, client id and secret.
SERVICE_PRINCIPAL_PROVIDER = "fabric_sp"

_GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_EARLY_REFRESH_SECONDS = 300
_MAX_CACHED_TOKENS = 1000
# Client-credentials errors that mean the stored app or secret no longer works.
_DEAD = {"invalid_client", "unauthorized_client", "invalid_grant"}


class ServicePrincipalRejected(Exception):
    """Microsoft refused the service principal. The message is safe to show."""


def check_service_principal_fields(tenant_id: str, client_id: str,
                                   client_secret: str) -> tuple[str, str, str]:
    """The three values, trimmed and shape-checked (ServicePrincipalRejected otherwise)."""
    tenant_id, client_id = tenant_id.strip().lower(), client_id.strip().lower()
    client_secret = client_secret.strip()
    if not _GUID.match(tenant_id):
        raise ServicePrincipalRejected("The Directory (tenant) ID is a GUID like "
                                       "11111111-2222-3333-4444-555555555555.")
    if not _GUID.match(client_id):
        raise ServicePrincipalRejected("The Application (client) ID is a GUID like "
                                       "11111111-2222-3333-4444-555555555555.")
    if not client_secret or len(client_secret) > 512 or any(c in client_secret for c in "\r\n\t"):
        raise ServicePrincipalRejected("Enter the client secret's Value (not its Secret ID).")
    return tenant_id, client_id, client_secret


class ServicePrincipalTokens:
    """
    Access tokens for a service principal (client-credentials grant), per scope. Same
    interface as the other token providers, so the pipeline treats it like any sign-in.
    """

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(timeout=30.0)
        self._cache: OrderedDict[tuple[str, int, str], tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def request(self, tenant_id: str, client_id: str, client_secret: str,
                scope: str) -> dict[str, Any]:
        try:
            response = self._http.post(
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": client_id,
                      "client_secret": client_secret, "scope": scope})
        except httpx.HTTPError:
            raise AdapterError("Couldn't reach Microsoft to sign the service principal in. "
                               "Try again.") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or "access_token" not in body:
            return {"error": body.get("error", f"http_{response.status_code}"),
                    "error_description": str(body.get("error_description", ""))}
        return body

    def check(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        """Prove the values work by asking for a Fabric API token. ServicePrincipalRejected."""
        from backend.core.configuration.app_registrations import explain_aadsts

        found = self.request(tenant_id, client_id, client_secret, FABRIC_API_SCOPE)
        if "error" in found:
            log.info("Service principal check refused: %s", found["error"])
            raise ServicePrincipalRejected(
                explain_aadsts(found.get("error_description", ""))
                or f"Microsoft refused the service principal ({found['error']}). Check the "
                   "tenant ID, client ID and secret.")

    def access_token(self, *, credential_id: str, version: int, scope: str,
                     refresh_token: str = "", secret: dict[str, Any] | None = None
                     ) -> AccessToken:
        key = (credential_id, version, scope)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[1] - _EARLY_REFRESH_SECONDS > time.time():
                self._cache.move_to_end(key)
                return AccessToken(cached[0], None)
        secret = secret or {}
        found = self.request(str(secret.get("tenant_id", "")), str(secret.get("client_id", "")),
                             str(secret.get("client_secret", "")), scope)
        if "error" in found:
            log.warning("Service principal token failed for credential %s: %s",
                        credential_id, found["error"])
            if found["error"] in _DEAD:
                raise CredentialRejected("Microsoft no longer accepts this service principal "
                                         "(its secret expired or the app was removed).")
            raise AdapterError("Microsoft refused a token for this service principal ("
                               f"{found['error']}). It may lack access to this resource.")
        expires = time.time() + float(found.get("expires_in", 3600))
        with self._lock:
            self._cache[key] = (found["access_token"], expires)
            while len(self._cache) > _MAX_CACHED_TOKENS:
                self._cache.popitem(last=False)
        return AccessToken(found["access_token"], None)
