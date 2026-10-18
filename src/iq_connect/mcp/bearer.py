"""
The front door of /mcp: no valid access token, no entry.

A missing or bad token gets `401` with `WWW-Authenticate: Bearer
resource_metadata="..."`. That header is how Claude discovers where to sign in
(it starts the whole OAuth flow from it), so its exact shape matters.

A valid token puts the caller (person, client, scope) into a context variable
for the duration of the request. Tools read it from there; nothing a tool
receives as an argument can change who the caller is.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..oauth.common import SCOPE
from ..security.tokens import AccessTokens, InvalidToken

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Caller:
    user_id: uuid.UUID
    client_id: str
    scope: frozenset[str]


CALLER: ContextVar[Caller | None] = ContextVar("iq_caller", default=None)


def current_caller() -> Caller:
    caller = CALLER.get()
    if caller is None:  # only reachable if a tool were served without the gate
        raise RuntimeError("tool called without an authenticated caller")
    return caller


class McpGate:
    def __init__(self, inner: ASGIApp, tokens: AccessTokens, *, audience: str,
                 metadata_url: str) -> None:
        self._inner = inner
        self._tokens = tokens
        self._audience = audience
        self._metadata_url = metadata_url

    def _refuse(self, status: int, error: str | None, description: str) -> JSONResponse:
        challenge = f'Bearer resource_metadata="{self._metadata_url}"'
        if error:
            challenge += f', error="{error}"'
        if error == "insufficient_scope":
            challenge += f', scope="{SCOPE}"'
        return JSONResponse(
            {"error": error or "invalid_request", "error_description": description},
            status_code=status,
            headers={"WWW-Authenticate": challenge},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        authorization = Headers(scope=scope).get("authorization", "")
        kind, _, token = authorization.partition(" ")
        if kind.lower() != "bearer" or not token.strip():
            # No error code when no token was sent at all (RFC 6750 3.1).
            await self._refuse(401, None, "Sign in to use this connector")(scope, receive, send)
            return
        try:
            claims = self._tokens.verify(token.strip(), audience=self._audience)
            caller = Caller(
                user_id=uuid.UUID(claims["sub"]),
                client_id=str(claims.get("client_id", "")),
                scope=frozenset(str(claims.get("scope", "")).split()),
            )
        except (InvalidToken, ValueError, KeyError) as error:
            log.info("Rejected access token: %s", error)
            await self._refuse(401, "invalid_token", "The access token is invalid or expired")(
                scope, receive, send
            )
            return
        if SCOPE not in caller.scope:
            await self._refuse(403, "insufficient_scope", f"The token lacks scope {SCOPE}")(
                scope, receive, send
            )
            return

        reset = CALLER.set(caller)
        try:
            await self._inner(scope, receive, send)
        finally:
            CALLER.reset(reset)
