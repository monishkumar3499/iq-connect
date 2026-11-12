"""
/auth/login, /auth/callback/<provider>, and the dev sign-in (its form is a screen of
the React app; this module only takes the post). Sign-out is /api/logout.

Failures redirect to the app's error page with the reason in a signed cookie
(web/common.py::error_page), never as text in the address.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from backend.core.auth.identity import DevProvider, SignInFailed
from backend.core.auth.users import sign_in
from backend.core.connection_manager.credentials import save_link
from backend.core.web.common import error_page

log = logging.getLogger(__name__)
router = APIRouter(tags=["sign-in"])


def safe_next(target: str | None) -> str:
    """
    Only ever continue to a page on this site. `next` arrives in a link anyone
    can craft; following it elsewhere would make our sign-in page a launch pad
    for phishing ("sign in, then land on the attacker's look-alike").
    """
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return "/"
    return target


def login_url(next_path: str, platform: str = "") -> str:
    query = {"next": next_path, **({"platform": platform} if platform else {})}
    return "/auth/login?" + urlencode(query)


def _platform_scopes(state, platform: str) -> list[str]:
    """
    With Microsoft as our sign-in, the platform the person was setting up is granted in
    the same trip: Fabric, or Azure Databricks. Snowflake's scope depends on the person's
    own account, so it is asked for in its own step. Anything else: identity only.
    """
    if state.identity.name != "entra" or not state.entra.ready() or platform == "snowflake":
        return []
    plugin = state.platforms.plugin(platform)
    return plugin.microsoft_scopes(state, None) if plugin else []


@router.get("/auth/login")
def login(request: Request, next: str | None = None, platform: str = "") -> Response:
    state = request.app.state
    provider = state.identity
    if provider.name == "entra" and not provider.ready():
        # Nobody can sign in with Microsoft until its app registration is entered.
        return RedirectResponse("/app/setup/microsoft?" + urlencode({"next": safe_next(next)}),
                                status_code=302)
    callback = f"{state.settings.public_base_url}/auth/callback/{provider.name}"
    scopes = _platform_scopes(state, platform)
    destination, kept = provider.begin(callback, scopes)
    nonce = secrets.token_urlsafe(16)
    if provider.name == "dev":
        # Entra's own `state` (checked by MSAL) protects its round trip; the
        # dev form needs ours, so a callback we did not start -- signing
        # someone in as the attacker (login CSRF) -- is refused.
        destination += "&" + urlencode({"nonce": nonce})
    response = RedirectResponse(destination, status_code=302)
    state.sessions.keep_login_state(
        response, {"next": safe_next(next), "nonce": nonce, "provider": provider.name,
                   "link": bool(scopes), **kept}
    )
    return response


@router.get("/auth/callback/{provider_name}")
def callback(request: Request, provider_name: str) -> Response:
    state = request.app.state
    provider = state.identity
    response = RedirectResponse("/app", status_code=303)
    kept = state.sessions.take_login_state(request, response)
    if provider_name != provider.name or kept is None or kept.get("provider") != provider.name:
        return error_page(request, "Sign-in expired",
                          "Please start again from Claude or the sign-in link.")

    params = dict(request.query_params)
    if params.get("error"):
        from backend.core.configuration.app_registrations import explain_aadsts

        log.info("Identity provider returned %s", params.get("error"))
        reason = explain_aadsts(params.get("error_description", ""))
        return error_page(request, "Sign-in didn't complete",
                          reason or "The sign-in was not completed.")
    if provider.name == "dev" and not secrets.compare_digest(
        params.get("nonce", ""), kept.get("nonce", "")
    ):
        return error_page(request, "Sign-in expired", "Please start again.")

    try:
        identity = provider.finish(kept, params)
    except SignInFailed as error:
        return error_page(request, "Sign-in failed", str(error))

    if provider.name == "entra":
        # Proves an app entered in the setup UI: from now on only admins may replace it.
        state.microsoft.mark_used()
    with state.db.session() as session:
        # Every company's people may sign in; each company (Entra tenant) gets its own
        # org and each person their own user on the first sign-in.
        user = sign_in(session, identity)
        if identity.email.lower() in state.settings.admins:
            # Only the server's operators (ADMIN_EMAILS) are IQ admins: with any company
            # allowed in, "the first person to sign in" could be anyone.
            user.is_admin = True
        if kept.get("link") and identity.refresh_token:
            # The same Microsoft sign-in granted the platform: that is the person's link.
            save_link(session, state.secrets, user=user, provider="entra",
                      account=identity.tenant,
                      secret={"refresh_token": identity.refresh_token,
                              "subject": identity.subject, "username": identity.email})
        user_id = user.id
    log.info("Signed in user %s via %s%s", user_id, provider.name,
             " (platform access granted)" if kept.get("link") else "")

    response.headers["location"] = kept.get("next") or "/app"
    state.sessions.start(response, user_id)
    return response


# --- development sign-in -------------------------------------------------------


def _dev_provider(request: Request) -> DevProvider | None:
    provider = request.app.state.identity
    return provider if isinstance(provider, DevProvider) else None


@router.get("/auth/dev-login", include_in_schema=False)
def dev_login_form(request: Request, redirect_uri: str = "", nonce: str = "") -> Response:
    if _dev_provider(request) is None:
        return Response(status_code=404)
    # The form is the app's (it posts `nonce` back here); APP_ENV=dev only.
    return request.app.state.serve_app_page(request)


@router.post("/auth/dev-login", include_in_schema=False)
def dev_login_submit(
    request: Request, email: str = Form(), name: str = Form(""), nonce: str = Form("")
) -> Response:
    provider = _dev_provider(request)
    if provider is None:
        return Response(status_code=404)
    if "@" not in email:
        return error_page(request, "Sign-in failed", "Enter an email address.")
    query = urlencode({"code": provider.make_code(email, name), "nonce": nonce})
    return RedirectResponse(f"/auth/callback/dev?{query}", status_code=303)
