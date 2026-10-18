"""
/auth/platform/databricks/start and /callback: link the person's Databricks
account (Databricks OAuth on their workspace) so Databricks can be queried as
them. The person must already be signed in to our pages.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import update

from ..db.models import DataConnection, User
from ..identity.providers import SignInFailed
from ..identity.routes import login_url, safe_next
from ..web.pages import message
from .service import save_secret

log = logging.getLogger(__name__)
router = APIRouter(tags=["platform sign-in"])

_CALLBACK = "/auth/platform/databricks/callback"


@router.get("/auth/platform/databricks/start")
def databricks_start(request: Request, next: str | None = None) -> Response:
    state = request.app.state
    if state.databricks_oauth is None:
        return message("Not available", "This server is not set up for Databricks.", 404)
    if state.sessions.read(request) is None:
        return RedirectResponse(
            login_url(f"/auth/platform/databricks/start?next={safe_next(next)}"), status_code=302)
    destination, kept = state.databricks_oauth.begin_link(
        f"{state.settings.public_base_url}{_CALLBACK}")
    response = RedirectResponse(destination, status_code=302)
    state.sessions.keep_login_state(
        response, {"purpose": "link-databricks", "next": safe_next(next), **kept})
    return response


@router.get(_CALLBACK)
def databricks_callback(request: Request) -> Response:
    state = request.app.state
    browser = state.sessions.read(request)
    response = RedirectResponse("/", status_code=303)
    kept = state.sessions.take_login_state(request, response)
    if (state.databricks_oauth is None or browser is None or not kept
            or kept.get("purpose") != "link-databricks"):
        return message("Link expired", "Start signing in to Databricks again.")
    if request.query_params.get("error"):
        return message("Sign-in cancelled", "Your Databricks account was not linked.")
    try:
        linked = state.databricks_oauth.finish_link(kept, dict(request.query_params))
    except SignInFailed as error:
        return message("Sign-in failed", str(error), status_code=403)

    with state.db.session() as session:
        user = session.get(User, browser.user_id)
        if user is None:
            return message("Signed out", "Sign in again, then link Databricks.", 401)
        credential = save_secret(
            session, state.secrets, user=user, provider="databricks", account=linked.tenant,
            secret={"refresh_token": linked.refresh_token, "username": linked.username},
        )
        session.execute(
            update(DataConnection)
            .where(DataConnection.credential_id == credential.id)
            .values(status="active")
        )
        user_id = user.id
    log.info("User %s linked a Databricks account", user_id)
    response.headers["location"] = kept.get("next") or "/"
    return response
