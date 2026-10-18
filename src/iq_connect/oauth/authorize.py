"""
GET /oauth/authorize -- where Claude sends the person's browser to sign in.

The request is checked, saved as an OAuthFlow, and the browser goes on to our
own page (/connect, step 5) to sign in with SSO and pick connections. That
page finishes with `complete_flow` (or `deny_flow`), which sends the browser
back to Claude with a one-time code.

Error handling follows RFC 6749 section 4.1.2.1 exactly, because it matters:
while the client or its redirect address is unverified, errors are shown on
our page and NEVER redirected -- otherwise this endpoint would forward people
to any address an attacker puts in the link.
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ..db.models import OAuthCode, OAuthFlow
from . import common
from .clients import redirect_matches, resolve_client
from .common import describe_url, granted_scope, hash_secret, new_secret, with_query

log = logging.getLogger(__name__)
router = APIRouter(tags=["oauth"])

# Signing in with SSO and choosing connections can take a while.
FLOW_LIFETIME = timedelta(minutes=30)
# Claude exchanges the code within seconds; a short life limits a leaked one.
CODE_LIFETIME = timedelta(minutes=2)

_PKCE_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_SINGLE_VALUED = (
    "client_id", "redirect_uri", "response_type", "code_challenge",
    "code_challenge_method", "state", "scope", "resource",
)


class FlowNotFound(Exception):
    """The sign-in expired or was already finished."""


def _error_page(message: str) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset='utf-8'><title>Sign-in problem</title>"
        "<body style='font-family:system-ui;max-width:36rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>This sign-in link can't be used</h1><p>{html.escape(message)}</p>"
        "<p>Go back to Claude and try connecting again.</p></body>"
    )
    return HTMLResponse(body, status_code=400)


@router.get("/oauth/authorize")
def authorize(request: Request) -> Response:
    params = request.query_params
    if any(len(params.getlist(name)) > 1 for name in _SINGLE_VALUED):
        return _error_page("The link repeats a parameter.")

    state = request.app.state
    settings = state.settings
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")

    with state.db.session() as session:
        client = resolve_client(session, client_id, settings, state.cimd_fetch)
        if client is None:
            log.warning("Authorize refused: unknown client %s", client_id[:200])
            return _error_page("The application asking to connect is not recognised.")
        if not redirect_uri and len(client.redirect_uris) == 1:
            redirect_uri = client.redirect_uris[0]
        if not redirect_matches(redirect_uri, client.redirect_uris):
            log.warning("Authorize refused: redirect address not registered for %s", client_id)
            return _error_page("The return address is not registered for this application.")

        # From here on the client and its return address are trusted, so
        # errors go back to the client, which can tell the person.
        def fail(error: str, description: str) -> RedirectResponse:
            log.info("Authorize error for client %s: %s", client_id, error)
            target = with_query(
                redirect_uri, error=error, error_description=description,
                state=params.get("state"), iss=settings.issuer,
            )
            return RedirectResponse(target, status_code=302)

        if params.get("response_type") != "code":
            return fail("unsupported_response_type", "Only response_type=code is supported")
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not _PKCE_CHALLENGE.match(challenge):
            return fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
        resource = params.get("resource") or settings.mcp_url
        if resource != settings.mcp_url:
            return fail("invalid_target", "This server only issues tokens for its own /mcp")

        flow = OAuthFlow(
            id=new_secret(),
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            state=params.get("state"),
            code_challenge=challenge,
            scope=granted_scope(params.get("scope")),
            resource=resource,
            created_at=common.now(),
            expires_at=common.now() + FLOW_LIFETIME,
        )
        session.add(flow)

    log.info("Sign-in started for client %s (%s)", client.client_id, client.name or "unnamed")
    return RedirectResponse(with_query("/connect", flow=flow.id), status_code=302)


def complete_flow(session: Session, flow_id: str, user_id: uuid.UUID, issuer: str) -> str:
    """
    Finish a sign-in for a signed-in person: issue a one-time code and return
    the address to send their browser to (back to Claude).
    """
    flow = session.get(OAuthFlow, flow_id)
    if flow is None or flow.expires_at <= common.now():
        raise FlowNotFound(flow_id[:6])
    code = new_secret()
    session.add(OAuthCode(
        code_hash=hash_secret(code),
        client_id=flow.client_id,
        user_id=user_id,
        redirect_uri=flow.redirect_uri,
        code_challenge=flow.code_challenge,
        scope=flow.scope,
        resource=flow.resource,
        expires_at=common.now() + CODE_LIFETIME,
    ))
    session.delete(flow)
    log.info("Returning to %s for client %s", describe_url(flow.redirect_uri), flow.client_id)
    return with_query(flow.redirect_uri, code=code, state=flow.state, iss=issuer)


def deny_flow(session: Session, flow_id: str, issuer: str) -> str:
    """The person cancelled: tell Claude so, and forget the sign-in."""
    flow = session.get(OAuthFlow, flow_id)
    if flow is None or flow.expires_at <= common.now():
        raise FlowNotFound(flow_id[:6])
    session.delete(flow)
    return with_query(
        flow.redirect_uri, error="access_denied", error_description="The sign-in was cancelled",
        state=flow.state, iss=issuer,
    )
