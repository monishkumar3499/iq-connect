"""
/auth/platform/entra/start and /callback: link the person's Microsoft account
so Fabric can be queried as them.

The person must already be signed in to our pages. When our own sign-in is
also Entra, the linked Microsoft account must be the same person: queries run
"as the user", and that must mean the user who is signed in, not whichever
account they happened to pick in Microsoft's account chooser.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import update

from ..adapters.databricks.adapter import AZURE_DATABRICKS_SCOPE
from ..db.models import DataConnection, User
from ..identity.providers import SignInFailed
from ..identity.routes import login_url, safe_next
from ..web.pages import message
from .service import save_secret

log = logging.getLogger(__name__)
router = APIRouter(tags=["platform sign-in"])

_CALLBACK = "/auth/platform/entra/callback"


@router.get("/auth/platform/entra/start")
def link_start(request: Request, next: str | None = None, platform: str = "") -> Response:
    state = request.app.state
    if state.entra is None:
        return message("Not available", "This server is not set up for Microsoft sign-in.", 404)
    if state.sessions.read(request) is None:
        again = "/auth/platform/entra/start?" + urlencode(
            {"next": safe_next(next), **({"platform": platform} if platform else {})})
        return RedirectResponse(login_url(again), status_code=302)
    # Asking for the platform's own resource makes Microsoft show its consent.
    scopes = [AZURE_DATABRICKS_SCOPE] if platform == "databricks" else None
    destination, kept = state.entra.begin_link(
        f"{state.settings.public_base_url}{_CALLBACK}", scopes)
    response = RedirectResponse(destination, status_code=302)
    state.sessions.keep_login_state(
        response, {"purpose": "link-entra", "next": safe_next(next), **kept}
    )
    return response


@router.get(_CALLBACK)
def link_callback(request: Request) -> Response:
    state = request.app.state
    browser = state.sessions.read(request)
    response = RedirectResponse("/", status_code=303)
    kept = state.sessions.take_login_state(request, response)
    if state.entra is None or browser is None or not kept or kept.get("purpose") != "link-entra":
        return message("Link expired", "Start linking your Microsoft account again.")
    if request.query_params.get("error"):
        return message("Link cancelled", "Your Microsoft account was not linked.")

    try:
        linked = state.entra.finish_link(kept, dict(request.query_params))
    except SignInFailed as error:
        return message("Link failed", str(error), status_code=403)

    with state.db.session() as session:
        user = session.get(User, browser.user_id)
        if user is None:
            return message("Signed out", "Sign in again, then link your account.", 401)
        if state.settings.sso_provider == "entra" and linked.subject != user.idp_subject:
            log.warning("User %s tried to link a different Microsoft account", user.id)
            return message(
                "Different account",
                "Link the same Microsoft account you signed in to IQ Connect with.", 403,
            )
        credential = save_secret(
            session, state.secrets, user=user, provider="entra", account=linked.tenant,
            secret={"refresh_token": linked.refresh_token, "subject": linked.subject,
                    "username": linked.username},
        )
        # Linking again repairs connections that were waiting for a new sign-in.
        session.execute(
            update(DataConnection)
            .where(DataConnection.credential_id == credential.id)
            .values(status="active")
        )
        user_id = user.id
    log.info("User %s linked a Microsoft account for Fabric", user_id)
    response.headers["location"] = kept.get("next") or "/"
    return response
