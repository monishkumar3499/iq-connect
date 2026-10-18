"""
/auth/login, /auth/callback/<provider>, /auth/logout, and the dev sign-in form.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ..web.pages import escape, message, page
from .providers import DevProvider, SignInFailed
from .users import sign_in

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


def login_url(next_path: str) -> str:
    return "/auth/login?" + urlencode({"next": next_path})


@router.get("/auth/login")
def login(request: Request, next: str | None = None) -> Response:
    state = request.app.state
    provider = state.identity
    callback = f"{state.settings.public_base_url}/auth/callback/{provider.name}"
    destination, kept = provider.begin(callback)
    nonce = secrets.token_urlsafe(16)
    if provider.name == "dev":
        # Entra's own `state` (checked by MSAL) protects its round trip; the
        # dev form needs ours, so a callback we did not start -- signing
        # someone in as the attacker (login CSRF) -- is refused.
        destination += "&" + urlencode({"nonce": nonce})
    response = RedirectResponse(destination, status_code=302)
    state.sessions.keep_login_state(
        response, {"next": safe_next(next), "nonce": nonce, "provider": provider.name, **kept}
    )
    return response


@router.get("/auth/callback/{provider_name}")
def callback(request: Request, provider_name: str) -> Response:
    state = request.app.state
    provider = state.identity
    response = RedirectResponse("/", status_code=303)
    kept = state.sessions.take_login_state(request, response)
    if provider_name != provider.name or kept is None or kept.get("provider") != provider.name:
        return message("Sign-in expired", "Please start again from Claude or the sign-in link.")

    params = dict(request.query_params)
    if params.get("error"):
        log.info("Identity provider returned %s", params.get("error"))
        return message("Sign-in cancelled", "The sign-in was not completed.")
    if provider.name == "dev" and not secrets.compare_digest(
        params.get("nonce", ""), kept.get("nonce", "")
    ):
        return message("Sign-in expired", "Please start again.")

    try:
        identity = provider.finish(kept, params)
    except SignInFailed as error:
        return message("Sign-in failed", str(error), status_code=403)

    with state.db.session() as session:
        user = sign_in(session, identity)
        user_id = user.id
    log.info("Signed in user %s via %s", user_id, provider.name)

    response.headers["location"] = kept.get("next") or "/"
    state.sessions.start(response, user_id)
    return response


@router.post("/auth/logout")
def logout(request: Request, csrf: str = Form("")) -> Response:
    # The CSRF token stops another site from signing people out behind their backs.
    browser = request.app.state.sessions.read(request)
    if browser is not None and not browser.csrf_matches(csrf):
        return message("Request refused", "Reload the page and try again.", 403)
    response = RedirectResponse("/", status_code=303)
    request.app.state.sessions.end(response)
    return response


# --- development sign-in -------------------------------------------------------


def _dev_provider(request: Request) -> DevProvider | None:
    provider = request.app.state.identity
    return provider if isinstance(provider, DevProvider) else None


@router.get("/auth/dev-login", include_in_schema=False)
def dev_login_form(request: Request, redirect_uri: str = "", nonce: str = "") -> Response:
    if _dev_provider(request) is None:
        return Response(status_code=404)
    return page("Development sign-in", (
        "<h1>Development sign-in</h1>"
        "<p class='muted'>No password: this screen only exists while APP_ENV=dev. "
        "With Entra configured, people see Microsoft's sign-in instead.</p>"
        "<form method='post' action='/auth/dev-login'>"
        f"<input type='hidden' name='nonce' value='{escape(nonce)}'>"
        "<label for='email'>Email</label>"
        "<input id='email' name='email' type='email' required value='alice@example.com'>"
        "<label for='name'>Name</label>"
        "<input id='name' name='name' type='text' value='Alice'>"
        "<div class='row'><button class='primary' type='submit'>Sign in</button></div>"
        "</form>"
    ))


@router.post("/auth/dev-login", include_in_schema=False)
def dev_login_submit(
    request: Request, email: str = Form(), name: str = Form(""), nonce: str = Form("")
) -> Response:
    provider = _dev_provider(request)
    if provider is None:
        return Response(status_code=404)
    if "@" not in email:
        return message("Sign-in failed", "Enter an email address.")
    query = urlencode({"code": provider.make_code(email, name), "nonce": nonce})
    return RedirectResponse(f"/auth/callback/dev?{query}", status_code=303)
