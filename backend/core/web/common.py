"""
What every JSON endpoint behind the React app shares: who is asking, the CSRF
check on changes, errors as JSON, and Claude's sign-in request (`flow`).

Rules, each for a reason:
  - Reads need the session cookie; changes also need the session's CSRF token in
    the `X-CSRF-Token` header and a JSON body. Another site can't read the token
    (no CORS), and can't send JSON cross-site without a CORS preflight we never allow.
  - Only ids come from the browser. Names, schemas and workspaces are looked up
    again as the person before anything is stored (setup/resources.py).
  - Errors are short sentences for the person, never stack traces.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from backend.core.auth.browser_sessions import BrowserSession
from backend.core.auth.oauth import common
from backend.shared.models import OAuthClient, OAuthFlow, User

_FLOW = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
MAX_BODY = 64 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", **extra: Any) -> None:
        super().__init__(message or code)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def api_error_response(_: Request, error: ApiError) -> JSONResponse:
    return JSONResponse({"error": error.code, "message": error.message, **error.extra},
                        status_code=error.status)


def flow_id(value: str | None) -> str:
    """A Claude sign-in id to carry along, or "" -- never anything else."""
    return value if value and _FLOW.match(value) else ""


def with_query(path: str, **params: str) -> str:
    """`path` with the non-empty `params` as its query string."""
    query = {k: v for k, v in params.items() if v}
    return path + ("?" + urlencode(query) if query else "")


def person(request: Request) -> BrowserSession:
    """The signed-in browser, or 401 (the app then sends the person to sign in)."""
    browser = request.app.state.sessions.read(request)
    if browser is None:
        raise ApiError(401, "signed_out", "Sign in to continue.")
    return browser


async def change(request: Request) -> tuple[BrowserSession, dict[str, Any]]:
    """(browser, JSON body) for a request that changes something; refuses forgeries."""
    browser = person(request)
    if not browser.csrf_matches(request.headers.get("x-csrf-token")):
        raise ApiError(403, "csrf", "Reload the page and try again.")
    return browser, await json_body(request)


async def json_body(request: Request) -> dict[str, Any]:
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise ApiError(415, "json_only", "Send JSON.")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ApiError(413, "too_large", "That is too long.")
    try:
        data = json.loads(raw or b"{}")
    except ValueError as error:
        raise ApiError(400, "bad_json", "Send JSON.") from error
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json", "Send a JSON object.")
    return data


def text(data: dict[str, Any], key: str, limit: int = 200) -> str:
    value = data.get(key, "")
    return value[:limit] if isinstance(value, str) else ""


def texts(data: dict[str, Any], key: str, *, limit: int = 50, each: int = 200) -> list[str]:
    """A de-duplicated, bounded list of strings from the body."""
    values = data.get(key, [])
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(v)[:each] for v in values if isinstance(v, str)))[:limit]


def parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def is_admin(request: Request, browser: BrowserSession | None) -> bool:
    if browser is None:
        return False
    with request.app.state.db.session() as session:
        user = session.get(User, browser.user_id)
        return bool(user and user.is_admin)


def error_page(request: Request, title: str, text_: str) -> RedirectResponse:
    """For the sign-in redirects: show `text_` on the app's error page, via a signed cookie."""
    response = RedirectResponse("/error", status_code=303)
    request.app.state.sessions.flash(response, title, text_)
    return response


# --- Claude's sign-in request ----------------------------------------------------------------


def bind_flow(request: Request, flow: str, user_id: uuid.UUID) -> None:
    """
    Remember who is completing this sign-in. Finish refuses anyone else, so a
    browser that changes hands mid-setup can't approve Claude as a different
    person than the one who picked the resources.
    """
    if not flow:
        return
    with request.app.state.db.session() as session:
        row = session.get(OAuthFlow, flow)
        if row is not None and row.user_id is None and row.expires_at > common.now():
            row.user_id = user_id


def flow_info(request: Request, flow: str) -> dict[str, Any] | None:
    """The live Claude sign-in that `flow` names (bound user, app name), or None."""
    if not flow:
        return None
    with request.app.state.db.session() as session:
        row = session.get(OAuthFlow, flow)
        if row is None or row.expires_at <= common.now():
            return None
        client = session.get(OAuthClient, row.client_id)
        name = (client.name if client and client.name else "") or "Claude"
        return {"user_id": row.user_id, "app": name}
