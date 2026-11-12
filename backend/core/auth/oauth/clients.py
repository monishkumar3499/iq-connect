"""
Who may sign in through us, and where we may send them back.

Two ways a client (Claude) identifies itself:

  DCR   It registers first (POST /oauth/register) and gets a client_id from us.
  CIMD  Its client_id is an HTTPS URL to a JSON document describing it, which
        we fetch -- but only from an allow-list of hosts (claude.ai,
        claude.com). Fetching any other URL a caller supplies would turn this
        server into a tool for reaching internal addresses (SSRF).

Redirect addresses are restricted too: a registered client may only send
people back to an allowed host or to the person's own machine (loopback).
Otherwise anyone could register a client that delivers sign-in codes to a
site they control.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.core.auth.oauth import common
from backend.core.auth.oauth.common import NO_STORE, OAuthError
from backend.core.configuration.settings import Settings
from backend.shared.models import OAuthClient

log = logging.getLogger(__name__)
router = APIRouter(tags=["oauth"])

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SUPPORTED_GRANTS = ("authorization_code", "refresh_token")
MAX_REDIRECT_URIS = 10
MAX_URI_LENGTH = 2000
CIMD_MAX_BYTES = 64 * 1024
CIMD_CACHE = timedelta(hours=1)

CimdFetcher = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class ClientInfo:
    client_id: str
    kind: str
    name: str
    redirect_uris: tuple[str, ...]


class CimdError(Exception):
    pass


# ---------------------------------------------------------------------------
# Redirect URI rules
# ---------------------------------------------------------------------------


def is_loopback(uri: str) -> bool:
    parts = urlsplit(uri)
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def redirect_allowed(uri: str, allowed_hosts: frozenset[str]) -> bool:
    """May a client register this redirect address?"""
    if not uri or len(uri) > MAX_URI_LENGTH:
        return False
    parts = urlsplit(uri)
    if parts.fragment or parts.username or parts.password:
        return False
    if is_loopback(uri):
        return True
    return parts.scheme == "https" and (parts.hostname or "").lower() in allowed_hosts


def redirect_matches(requested: str, registered: tuple[str, ...]) -> bool:
    """
    Is `requested` one of the client's registered addresses?

    Exact match, except loopback: a native app (Claude Code) listens on
    whatever port is free at the time, so RFC 8252 says the port must not be
    part of the comparison for loopback addresses.
    """
    if requested in registered:
        return True
    if not is_loopback(requested):
        return False
    want = urlsplit(requested)
    for uri in registered:
        have = urlsplit(uri)
        if is_loopback(uri) and (have.hostname, have.path, have.query) == (
            want.hostname, want.path, want.query,
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Looking a client up
# ---------------------------------------------------------------------------


def cimd_url_allowed(client_id: str, settings: Settings) -> bool:
    if len(client_id) > 500:
        return False
    parts = urlsplit(client_id)
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() in settings.cimd_hosts
        and parts.port is None
        and not parts.username
        and not parts.password
        and not parts.fragment
        and parts.path not in ("", "/")
    )


def fetch_cimd_document(url: str) -> dict[str, Any]:
    """Download a Client ID Metadata Document. Small, fast, no redirects."""
    with httpx.Client(timeout=5.0, follow_redirects=False) as client:
        with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
            if response.status_code != 200:
                raise CimdError(f"status {response.status_code}")
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > CIMD_MAX_BYTES:
                    raise CimdError("document too large")
    try:
        document = json.loads(body)
    except ValueError as error:
        raise CimdError("not JSON") from error
    if not isinstance(document, dict):
        raise CimdError("not a JSON object")
    return document


def _validate_cimd(url: str, document: dict[str, Any], settings: Settings) -> ClientInfo:
    if document.get("client_id") != url:
        raise CimdError("client_id in the document does not match its URL")
    uris = document.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECT_URIS:
        raise CimdError("redirect_uris missing or invalid")
    if not all(isinstance(u, str) and redirect_allowed(u, settings.redirect_hosts) for u in uris):
        raise CimdError("redirect_uris contain a disallowed address")
    name = document.get("client_name")
    return ClientInfo(url, "cimd", name if isinstance(name, str) else "", tuple(uris))


def resolve_client(
    session: Session, client_id: str, settings: Settings, fetch: CimdFetcher
) -> ClientInfo | None:
    """The client behind a client_id, or None if we don't know or trust it."""
    if not client_id:
        return None
    if client_id.startswith("https://"):
        return _resolve_cimd(session, client_id, settings, fetch)
    row = session.get(OAuthClient, client_id)
    if row is None or row.kind != "dcr":
        return None
    return ClientInfo(row.client_id, row.kind, row.name, tuple(row.redirect_uris))


def _resolve_cimd(
    session: Session, url: str, settings: Settings, fetch: CimdFetcher
) -> ClientInfo | None:
    if not settings.oauth_cimd_enabled or not cimd_url_allowed(url, settings):
        return None
    row = session.get(OAuthClient, url)
    if row is not None and row.refresh_after and row.refresh_after > common.now():
        return ClientInfo(row.client_id, row.kind, row.name, tuple(row.redirect_uris))
    try:
        info = _validate_cimd(url, fetch(url), settings)
    except Exception as error:
        if row is not None:
            # claude.ai being briefly unreachable must not block sign-ins for
            # a client we already validated; the cached copy stays in use.
            log.warning("Could not refresh client document %s (%s); using cached copy", url, error)
            return ClientInfo(row.client_id, row.kind, row.name, tuple(row.redirect_uris))
        log.warning("Rejected client document %s: %s", url, error)
        return None
    if row is None:
        row = OAuthClient(client_id=url, kind="cimd")
        session.add(row)
    row.name, row.redirect_uris = info.name, list(info.redirect_uris)
    row.refresh_after = common.now() + CIMD_CACHE
    return info


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------


class _RegistrationRequest(BaseModel):
    redirect_uris: list[str]
    client_name: str = ""
    grant_types: list[str] = list(SUPPORTED_GRANTS)
    response_types: list[str] = ["code"]
    # Whatever the client asks for, it is registered as a public client
    # ("none"); RFC 7591 lets the server substitute values, and the client
    # must use what we return. We never issue client secrets.
    token_endpoint_auth_method: str = "none"


@router.post("/oauth/register", status_code=201)
async def register(request: Request) -> JSONResponse:
    try:
        body = _RegistrationRequest.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise OAuthError(
            "invalid_client_metadata", "Body must be a JSON registration request"
        ) from None

    settings = request.app.state.settings
    if not body.redirect_uris or len(body.redirect_uris) > MAX_REDIRECT_URIS:
        raise OAuthError("invalid_redirect_uri", "Provide between 1 and 10 redirect_uris")
    for uri in body.redirect_uris:
        if not redirect_allowed(uri, settings.redirect_hosts):
            raise OAuthError("invalid_redirect_uri", f"Redirect address not allowed: {uri[:200]}")
    if not set(body.grant_types) <= set(SUPPORTED_GRANTS) or body.response_types != ["code"]:
        raise OAuthError("invalid_client_metadata", "Only the authorization code flow is supported")

    client = OAuthClient(
        client_id="dcr_" + common.new_secret(),
        kind="dcr",
        name=body.client_name[:200],
        redirect_uris=body.redirect_uris,
        created_at=common.now(),
    )
    def save() -> None:
        # Committed before we answer: the client may use its id immediately.
        with request.app.state.db.session() as session:
            session.add(client)

    await run_in_threadpool(save)
    log.info("Registered client %s (%s)", client.client_id, client.name or "unnamed")
    return JSONResponse(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at.timestamp()),
            "client_name": client.name,
            "redirect_uris": client.redirect_uris,
            "grant_types": list(SUPPORTED_GRANTS),
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
        headers=NO_STORE,
    )
