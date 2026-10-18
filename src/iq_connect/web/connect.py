"""
/connect -- the page Claude's sign-in lands on (via /oauth/authorize).

The person signs in (SSO), sees which app is asking and as whom they are
connecting, and presses Allow or Cancel. Allow finishes the OAuth flow and
sends the browser back to Claude with a one-time code.

Connections are not chosen here: Claude's token identifies the person, and
every tool call names a connection. This page lists the person's
connections for reassurance and will link to the setup wizard (step 11).
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from ..db.models import DataConnection, OAuthClient, OAuthFlow, User
from ..identity.routes import login_url
from ..oauth import common
from ..oauth.authorize import FlowNotFound, complete_flow, deny_flow
from .pages import escape, message, page

log = logging.getLogger(__name__)
router = APIRouter(tags=["connect"])


def _client_label(session, client_id: str) -> str:
    client = session.get(OAuthClient, client_id)
    return (client.name if client and client.name else "") or "An application"


@router.get("/connect")
def connect_page(request: Request, flow: str = "") -> Response:
    state = request.app.state
    browser = state.sessions.read(request)
    if browser is None:
        return RedirectResponse(login_url(f"/connect?flow={flow}"), status_code=302)

    with state.db.session() as session:
        row = session.get(OAuthFlow, flow) if flow else None
        if row is None or row.expires_at <= common.now():
            return message("Sign-in expired",
                           "This connection request has expired. Go back to Claude and "
                           "press Connect again.")
        user = session.get(User, browser.user_id)
        if user is None:
            response = RedirectResponse(login_url(f"/connect?flow={flow}"), status_code=302)
            state.sessions.end(response)
            return response
        app_name = _client_label(session, row.client_id)
        connections = session.scalars(
            select(DataConnection)
            .where(DataConnection.user_id == user.id)
            .order_by(DataConnection.name)
        ).all()
        listed = "".join(
            f"<li>{escape(c.name)} <span class='muted'>({escape(c.platform)})</span></li>"
            for c in connections
        )
        who = escape(user.email)

    new_link = escape("/app/connections/new?" + urlencode({"flow": flow}))
    connections_html = (
        f"<p>Your connections:</p><ul>{listed}</ul>"
        f"<p><a href='{new_link}'>+ New connection</a></p>" if listed else
        "<p class='muted'>You have no data connections yet.</p>"
        f"<p><a href='{new_link}'>+ Create your first connection</a> "
        "<span class='muted'>(you come back here afterwards)</span></p>"
    )
    hidden = (
        f"<input type='hidden' name='flow' value='{escape(flow)}'>"
        f"<input type='hidden' name='csrf' value='{escape(browser.csrf)}'>"
    )
    return page("Connect to Claude", (
        f"<h1>Connect {escape(app_name)} to IQ</h1>"
        f"<p>{escape(app_name)} will be able to query your data connections as "
        f"<strong>{who}</strong>, read-only, within the scope you set for each one.</p>"
        f"{connections_html}"
        "<div class='row'>"
        f"<form method='post' action='/connect/approve'>{hidden}"
        "<button class='primary' type='submit'>Allow</button></form>"
        f"<form method='post' action='/connect/deny'>{hidden}"
        "<button type='submit'>Cancel</button></form>"
        "</div>"
        "<p class='muted'>Not you? <a href='"
        + escape(login_url(f"/connect?flow={flow}"))
        + "'>Sign in as someone else</a>.</p>"
    ))


def _decide(request: Request, flow: str, csrf: str, allow: bool) -> Response:
    state = request.app.state
    browser = state.sessions.read(request)
    if browser is None:
        return message("Signed out", "Your session ended. Go back to Claude and try again.", 401)
    if not browser.csrf_matches(csrf):
        # A form posted from another site, or a stale page.
        return message("Request refused", "Please reload the page and try again.", 403)
    try:
        with state.db.session() as session:
            if allow:
                target = complete_flow(session, flow, browser.user_id, state.settings.issuer)
            else:
                target = deny_flow(session, flow, state.settings.issuer)
    except FlowNotFound:
        return message("Sign-in expired",
                       "This connection request has expired. Go back to Claude and "
                       "press Connect again.")
    # 303: the browser follows with a GET, as Claude's callback expects.
    return RedirectResponse(target, status_code=303)


@router.post("/connect/approve")
def approve(request: Request, flow: str = Form(), csrf: str = Form("")) -> Response:
    return _decide(request, flow, csrf, allow=True)


@router.post("/connect/deny")
def deny(request: Request, flow: str = Form(), csrf: str = Form("")) -> Response:
    return _decide(request, flow, csrf, allow=False)
