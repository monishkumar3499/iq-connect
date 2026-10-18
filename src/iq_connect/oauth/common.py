"""Small pieces shared by the OAuth endpoints."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi.responses import JSONResponse

from ..db.types import utcnow

# The one scope this server grants. Unknown scopes a client asks for are
# ignored rather than refused: they grant nothing, and refusing would break a
# sign-in over a name (Claude sends whatever our metadata or its defaults say).
SCOPE = "iq"

# Token responses carry credentials; no proxy or browser may keep a copy.
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def now() -> datetime:
    """Current time. A module function so tests can move the clock."""
    return utcnow()


class OAuthError(Exception):
    """An error answered in the RFC 6749 JSON shape."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(f"{error}: {description}")
        self.error = error
        self.description = description
        self.status = status


def oauth_error_response(error: OAuthError) -> JSONResponse:
    return JSONResponse(
        {"error": error.error, "error_description": error.description},
        status_code=error.status,
        headers=NO_STORE,
    )


def new_secret() -> str:
    """An unguessable value for codes, refresh tokens and flow ids (256 bits)."""
    return secrets.token_urlsafe(32)


def hash_secret(value: str) -> str:
    """What we store instead of a code or token: a leaked table can't be replayed."""
    return hashlib.sha256(value.encode()).hexdigest()


def granted_scope(requested: str | None) -> str:
    return SCOPE


def with_query(url: str, **params: str | None) -> str:
    """Add parameters to a URL, keeping any it already has."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [(key, value) for key, value in params.items() if value is not None]
    return urlunsplit(parts._replace(query=urlencode(query)))


def describe_url(url: str) -> str:
    """A URL without its query string, safe to log."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(query="", fragment=""))
