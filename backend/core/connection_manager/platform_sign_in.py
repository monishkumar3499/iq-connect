"""
Linking a person's platform account, so queries run as them:

  /auth/platform/entra/start, /callback       the Microsoft account: Fabric, Azure
                                              Databricks (?platform=databricks) or
                                              Snowflake External OAuth (?platform=snowflake,
                                              with the account whose role scope to grant)
  /auth/platform/databricks/start, /callback  Databricks OAuth for one of the person's
                                              workspaces (?account=<their account id>)
  /auth/platform/snowflake/start, /callback   Snowflake OAuth + PKCE for one of the
                                              person's Snowflake accounts (?account=...)

The person must already be signed in to our pages. When our own sign-in is also Entra, the
linked Microsoft account must be the same person: queries run "as the user", and that must
mean the user who is signed in, not whichever account they happened to pick in Microsoft's
account chooser. An `account` id from the address is only ever looked up among the signed-in
person's own accounts.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from backend.core.auth.identity import SignInFailed
from backend.core.auth.sign_in import login_url, safe_next
from backend.core.connection_manager.credentials import read_secret, save_link
from backend.core.connection_manager.platform_accounts import (
    AccountInfo,
    account_by_id,
    attach_credential,
)
from backend.core.security.crypto import DecryptionError
from backend.core.web.common import error_page
from backend.shared.models import Credential, User

log = logging.getLogger(__name__)
router = APIRouter(tags=["platform sign-in"])

ENTRA_CALLBACK = "/auth/platform/entra/callback"
DATABRICKS_CALLBACK = "/auth/platform/databricks/callback"
SNOWFLAKE_CALLBACK = "/auth/platform/snowflake/callback"


def _begin(request: Request, purpose: str, destination: str, kept: dict,
           next_path: str | None) -> Response:
    response = RedirectResponse(destination, status_code=302)
    request.app.state.sessions.keep_login_state(
        response, {"purpose": purpose, "next": safe_next(next_path), **kept})
    return response


def _returning(request: Request, purpose: str):
    """(browser, kept state, response to finish with) or (None, None, error response)."""
    state = request.app.state
    browser = state.sessions.read(request)
    response = RedirectResponse("/app", status_code=303)
    kept = state.sessions.take_login_state(request, response)
    if browser is None or not kept or kept.get("purpose") != purpose:
        return None, None, error_page(request, "Link expired", "Start signing in again.")
    if request.query_params.get("error"):
        return None, None, error_page(request, "Sign-in cancelled",
                                      "Your account was not linked.")
    return browser, kept, response


def _signed_out(request: Request, path: str, **query: str) -> Response | None:
    """Not signed in to our pages: sign in first, then come back here."""
    if request.app.state.sessions.read(request) is not None:
        return None
    again = f"{path}?" + urlencode({k: v for k, v in query.items() if v})
    return RedirectResponse(login_url(again), status_code=302)


def _account(request: Request, account_id: str) -> AccountInfo | None:
    browser = request.app.state.sessions.read(request)
    if browser is None or not account_id:
        return None
    return account_by_id(request.app.state.db, browser.user_id, account_id)


def _account_secret(request: Request, account: AccountInfo) -> tuple[uuid.UUID, dict[str, Any]]:
    """The credential an OAuth account signs in with (its app details), or ValueError."""
    state = request.app.state
    with state.db.session() as session:
        credential = session.get(Credential, account.credential_id) \
            if account.credential_id else None
        browser = state.sessions.read(request)
        if credential is None or browser is None or credential.user_id != browser.user_id:
            raise ValueError("no credential")
        try:
            return credential.id, read_secret(state.secrets, credential)
        except DecryptionError:
            raise ValueError("unreadable") from None


# --- the Microsoft account ----------------------------------------------------------------------


@router.get("/auth/platform/entra/start")
def link_start(request: Request, next: str | None = None, platform: str = "",
               account: str = "") -> Response:
    state = request.app.state
    if not state.entra.ready():
        return error_page(request, "Not available",
                          "This server is not set up for Microsoft sign-in.")
    away = _signed_out(request, "/auth/platform/entra/start", next=safe_next(next),
                       platform=platform, account=account)
    if away is not None:
        return away
    # Asking for the platform's own resource makes Microsoft show its consent. Relinking
    # from Settings names no platform: that is Fabric's.
    chosen = _account(request, account)
    plugin = state.platforms.plugin(platform or "fabric")
    scopes = plugin.microsoft_scopes(state, chosen) if plugin else []
    if not scopes:
        return error_page(request, "Not available", "That platform can't be signed in to with "
                                                    "Microsoft here. Choose it again in Add a "
                                                    "new connection.")
    destination, kept = state.entra.begin_link(
        f"{state.settings.public_base_url}{ENTRA_CALLBACK}", scopes)
    kept["account"] = str(chosen.id) if chosen and chosen.id else ""
    return _begin(request, "link-entra", destination, kept, next)


@router.get(ENTRA_CALLBACK)
def link_callback(request: Request) -> Response:
    state = request.app.state
    if not state.entra.ready():
        return error_page(request, "Link expired", "Start signing in again.")
    browser, kept, response = _returning(request, "link-entra")
    if browser is None:
        return response
    try:
        linked = state.entra.finish_link(kept, dict(request.query_params))
    except SignInFailed as error:
        return error_page(request, "Link failed", str(error))

    with state.db.session() as session:
        user = session.get(User, browser.user_id)
        if user is None:
            return error_page(request, "Signed out", "Sign in again, then link your account.")
        if state.settings.sso_provider == "entra" and linked.subject != user.idp_subject:
            log.warning("User %s tried to link a different Microsoft account", user.id)
            return error_page(request, "Different account", "Link the same Microsoft account "
                                                            "you signed in to IQ Connect with.")
        # Linking again repairs connections that were waiting for a new sign-in.
        credential = save_link(session, state.secrets, user=user, provider="entra",
                               account=linked.tenant,
                               secret={"refresh_token": linked.refresh_token,
                                       "subject": linked.subject, "username": linked.username})
        user_id, credential_id = user.id, credential.id
    chosen = _account(request, str(kept.get("account", "")))
    if chosen is not None and chosen.id is not None:
        attach_credential(state.db, chosen.id, credential_id)
    log.info("User %s linked a Microsoft account", user_id)
    response.headers["location"] = kept.get("next") or "/app"
    return response


# --- Databricks OAuth and Snowflake OAuth: one of the person's own accounts --------------------


def _oauth_start(request: Request, platform: str, purpose: str, callback: str,
                 next_path: str | None, account: str) -> Response:
    state = request.app.state
    away = _signed_out(request, f"/auth/platform/{platform}/start", next=safe_next(next_path),
                       account=account)
    if away is not None:
        return away
    chosen = _account(request, account)
    if chosen is None or chosen.platform != platform:
        return error_page(request, "Sign-in expired", "Choose the platform again in Add a "
                                                      "new connection.")
    try:
        credential_id, secret = _account_secret(request, chosen)
    except ValueError:
        return error_page(request, "Sign-in details missing", "Enter the sign-in details again "
                                                              "in Add a new connection.")
    provider = getattr(state, f"{platform}_oauth")
    destination, kept = provider.begin_link(f"{state.settings.public_base_url}{callback}", secret)
    kept.update(account=str(chosen.id), credential=str(credential_id))
    return _begin(request, purpose, destination, kept, next_path)


def _oauth_callback(request: Request, platform: str, purpose: str, label: str) -> Response:
    state = request.app.state
    browser, kept, response = _returning(request, purpose)
    if browser is None:
        return response
    chosen = _account(request, str(kept.get("account", "")))
    if chosen is None or str(chosen.credential_id) != kept.get("credential"):
        return error_page(request, "Link expired", "Start signing in again.")
    try:
        _, secret = _account_secret(request, chosen)
    except ValueError:
        return error_page(request, "Link expired", "Start signing in again.")
    provider = getattr(state, f"{platform}_oauth")
    try:
        linked = provider.finish_link(kept, dict(request.query_params), secret)
    except SignInFailed as error:
        return error_page(request, "Sign-in failed", str(error))
    with state.db.session() as session:
        user = session.get(User, browser.user_id)
        if user is None:
            return error_page(request, "Signed out", f"Sign in again, then connect {label}.")
        credential = session.get(Credential, chosen.credential_id)
        if credential is None:
            return error_page(request, "Link expired", "Start signing in again.")
        # The same credential row (its app details), now with a refresh token. Linking
        # again repairs connections that were waiting for a new sign-in.
        save_link(session, state.secrets, user=user, provider=credential.provider,
                  account=credential.account,
                  secret={**secret, "refresh_token": linked.refresh_token,
                          "username": linked.username})
        user_id = user.id
    log.info("User %s linked a %s account", user_id, label)
    response.headers["location"] = kept.get("next") or "/app"
    return response


@router.get("/auth/platform/databricks/start")
def databricks_start(request: Request, next: str | None = None, account: str = "") -> Response:
    return _oauth_start(request, "databricks", "link-databricks", DATABRICKS_CALLBACK, next,
                        account)


@router.get(DATABRICKS_CALLBACK)
def databricks_callback(request: Request) -> Response:
    return _oauth_callback(request, "databricks", "link-databricks", "Databricks")


@router.get("/auth/platform/snowflake/start")
def snowflake_start(request: Request, next: str | None = None, account: str = "") -> Response:
    return _oauth_start(request, "snowflake", "link-snowflake", SNOWFLAKE_CALLBACK, next, account)


@router.get(SNOWFLAKE_CALLBACK)
def snowflake_callback(request: Request) -> Response:
    return _oauth_callback(request, "snowflake", "link-snowflake", "Snowflake")
