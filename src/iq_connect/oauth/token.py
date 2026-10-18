"""
POST /oauth/token  -- trade a one-time code, or a refresh token, for tokens.
POST /oauth/revoke -- Claude disconnects.

Several copies of the service may receive the same code or refresh token at
the same moment, so "use it" is always one conditional UPDATE. Whichever copy
changes the row wins; the other sees zero rows changed and refuses.

Refresh tokens rotate: each use returns a new one and retires the old one.
An old token showing up again means it was copied, so every token of that
person for that client is revoked and they must sign in again (OAuth 2.1
section 4.3.1). One exception, the grace period: if the old token is
presented again within a few seconds, it is almost certainly the client
retrying after our answer was lost on the network, not theft. Then the
undelivered successor is retired and a fresh one issued, so a flaky
connection never logs someone out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..db.models import OAuthCode, OAuthRefreshToken
from . import common
from .common import NO_STORE, OAuthError, hash_secret, new_secret

log = logging.getLogger(__name__)
router = APIRouter(tags=["oauth"])

REFRESH_RETRY_GRACE = timedelta(seconds=30)
_FORM = "application/x-www-form-urlencoded"


async def _read_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != _FORM:
        raise OAuthError("invalid_request", f"Send parameters as {_FORM}")
    form = await request.form()
    fields: dict[str, str] = {}
    for key in form.keys():
        values = form.getlist(key)
        if len(values) != 1 or not isinstance(values[0], str):
            raise OAuthError("invalid_request", f"Parameter {key!r} must appear once")
        fields[key] = values[0]
    return fields


def _require(fields: dict[str, str], *names: str) -> None:
    missing = [name for name in names if not fields.get(name)]
    if missing:
        raise OAuthError("invalid_request", f"Missing: {', '.join(missing)}")


@router.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    fields = await _read_form(request)
    grant = fields.get("grant_type")
    if grant == "authorization_code":
        _require(fields, "code", "client_id", "redirect_uri", "code_verifier")
        handler = _exchange_code
    elif grant == "refresh_token":
        _require(fields, "refresh_token", "client_id")
        handler = _refresh
    else:
        raise OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token")

    result = await run_in_threadpool(handler, request.app.state, fields)
    if isinstance(result, OAuthError):
        raise result
    return JSONResponse(result, headers=NO_STORE)


# Each handler returns the response body or an OAuthError instead of raising
# it, so that revocations made on the way to a refusal are still committed
# when the database session closes.


def _exchange_code(state: Any, fields: dict[str, str]) -> dict[str, Any] | OAuthError:
    client_id = fields["client_id"]
    log.info("Token exchange for client %s", client_id)
    with state.db.session() as session:
        code_hash = hash_secret(fields["code"])
        code = session.get(OAuthCode, code_hash)
        if code is None or code.client_id != client_id:
            return OAuthError("invalid_grant", "Unknown code")
        if code.expires_at <= common.now():
            return OAuthError("invalid_grant", "The code has expired")

        claimed = session.execute(
            update(OAuthCode)
            .where(OAuthCode.code_hash == code_hash, OAuthCode.used_at.is_(None))
            .values(used_at=common.now())
        ).rowcount
        if claimed != 1:
            # Second use of a code: it leaked. Revoke what the first use got.
            _revoke_all(session, code.user_id, client_id)
            log.warning("Code reused for client %s; revoked its tokens", client_id)
            return OAuthError("invalid_grant", "The code was already used")

        if fields["redirect_uri"] != code.redirect_uri:
            return OAuthError("invalid_grant", "redirect_uri does not match the sign-in")
        if not _pkce_matches(fields["code_verifier"], code.code_challenge):
            return OAuthError("invalid_grant", "code_verifier does not match")
        if fields.get("resource") and fields["resource"] != code.resource:
            return OAuthError("invalid_target", "resource does not match the sign-in")

        return _issue(state, session, user_id=code.user_id, client_id=client_id,
                      scope=code.scope, resource=code.resource or state.settings.mcp_url)


class _Abandon(Exception):
    """Leave the database session without committing anything, then refuse."""

    def __init__(self, error: OAuthError) -> None:
        self.error = error


def _refresh(state: Any, fields: dict[str, str]) -> dict[str, Any] | OAuthError:
    try:
        return _rotate(state, fields)
    except _Abandon as abandoned:
        return abandoned.error


def _rotate(state: Any, fields: dict[str, str]) -> dict[str, Any] | OAuthError:
    client_id = fields["client_id"]
    with state.db.session() as session:
        token_hash = hash_secret(fields["refresh_token"])
        row = session.get(OAuthRefreshToken, token_hash)
        if row is None or row.client_id != client_id:
            return OAuthError("invalid_grant", "Unknown refresh token")
        now = common.now()
        if row.expires_at <= now:
            return OAuthError("invalid_grant", "The refresh token has expired")

        previous_successor = row.replaced_by
        if row.revoked_at is not None and previous_successor is None:
            return OAuthError("invalid_grant", "The refresh token was revoked")
        if previous_successor is not None:
            if now - row.revoked_at > REFRESH_RETRY_GRACE:
                _revoke_all(session, row.user_id, client_id)
                log.warning("Old refresh token reused for client %s; revoked all", client_id)
                return OAuthError("invalid_grant", "The refresh token was already used")
            # A retry after a lost answer: retire the undelivered successor.
            _revoke_chain(session, previous_successor, now)

        requested = fields.get("scope")
        if requested and not set(requested.split()) <= set(row.scope.split()):
            return OAuthError("invalid_scope", "Cannot widen the scope on refresh")
        if fields.get("resource") and fields["resource"] != row.resource:
            return OAuthError("invalid_target", "resource does not match the sign-in")

        body = _issue(state, session, user_id=row.user_id, client_id=client_id,
                      scope=row.scope, resource=row.resource or state.settings.mcp_url)
        rotated = session.execute(
            update(OAuthRefreshToken)
            .where(
                OAuthRefreshToken.token_hash == token_hash,
                _same_successor(previous_successor),
            )
            .values(replaced_by=hash_secret(body["refresh_token"]), revoked_at=now)
        ).rowcount
        if rotated != 1:
            # Another request rotated this token in the meantime; refuse
            # rather than leave two live tokens. Nothing issued here is saved.
            raise _Abandon(OAuthError("invalid_grant", "The refresh token was just used; retry"))
        return body


def _same_successor(previous: str | None):
    column = OAuthRefreshToken.replaced_by
    return column.is_(None) if previous is None else column == previous


def _issue(
    state: Any, session: Session, *, user_id: Any, client_id: str, scope: str, resource: str
) -> dict[str, Any]:
    settings = state.settings
    refresh = new_secret()
    session.add(OAuthRefreshToken(
        token_hash=hash_secret(refresh),
        client_id=client_id,
        user_id=user_id,
        scope=scope,
        resource=resource,
        created_at=common.now(),
        expires_at=common.now() + timedelta(days=settings.refresh_token_days),
    ))
    access = state.access_tokens.issue(
        user_id=str(user_id), client_id=client_id, scope=scope, audience=resource
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": settings.access_token_minutes * 60,
        "refresh_token": refresh,
        "scope": scope,
    }


def _pkce_matches(verifier: str, challenge: str) -> bool:
    if not 43 <= len(verifier) <= 128:
        return False
    digest = hashlib.sha256(verifier.encode("ascii", errors="replace")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(expected, challenge)


def _revoke_chain(session: Session, token_hash: str | None, when) -> None:
    """Revoke a refresh token and every token that replaced it."""
    seen: set[str] = set()
    while token_hash and token_hash not in seen:
        seen.add(token_hash)
        row = session.get(OAuthRefreshToken, token_hash)
        if row is None:
            return
        if row.revoked_at is None:
            row.revoked_at = when
        token_hash = row.replaced_by


def _revoke_all(session: Session, user_id: Any, client_id: str) -> None:
    session.execute(
        update(OAuthRefreshToken)
        .where(
            OAuthRefreshToken.user_id == user_id,
            OAuthRefreshToken.client_id == client_id,
            OAuthRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=common.now())
    )


@router.post("/oauth/revoke")
async def revoke(request: Request) -> Response:
    """
    RFC 7009: always 200, whether or not the token existed, so the endpoint
    can't be used to test which tokens are valid. Access tokens are signed and
    simply expire; revoking the refresh token is what ends the connection.
    """
    fields = await _read_form(request)

    def work() -> None:
        with request.app.state.db.session() as session:
            row = session.get(OAuthRefreshToken, hash_secret(fields.get("token", "")))
            if row is not None and row.client_id == fields.get("client_id"):
                _revoke_chain(session, row.token_hash, common.now())
                log.info("Client %s revoked a refresh token", row.client_id)

    await run_in_threadpool(work)
    return Response(status_code=200, headers=NO_STORE)

