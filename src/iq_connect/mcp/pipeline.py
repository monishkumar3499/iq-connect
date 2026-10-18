"""
Every tool call that touches data goes through the same five steps:

  1 Who         the caller verified by the /mcp gate (never a tool argument)
  2 Connection  the named connection, looked up among THIS person's only
  3 Policy      the call is checked against the connection's scope and the
                read-only rules before any platform -- or Microsoft -- is contacted
  4 Credential  the person's stored platform login, decrypted for this call;
                for a linked Microsoft account, exchanged for short-lived access
                tokens so only those ever reach the adapter
  5 Run + audit on the platform's worker threads, with limits and a timeout;
                one audit row whatever the outcome

Failures become ToolError messages Claude can act on ("schema X is outside this
connection"). Unexpected failures are logged with a request id and reported
without internal details. A platform refusing the stored login marks every
connection using it as needing sign-in.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import update

from ..adapters.base import AdapterError, ConnectionContext, CredentialRejected, PlatformAdapter
from ..adapters.registry import AdapterRegistry, UnknownPlatform
from ..audit.logger import record
from ..connections import service as connections
from ..credentials.providers.entra import EntraTokens
from ..credentials.service import read_secret, refresh_secret
from ..db.models import Credential, DataConnection, User
from ..db.session import Database
from ..execution.pools import Busy, CallTimedOut, PlatformPools
from ..observability.logging import REQUEST_ID
from ..policy.sql_guard import QueryRejected
from ..security.crypto import DecryptionError, SecretBox
from .bearer import current_caller

log = logging.getLogger(__name__)
T = TypeVar("T")

Check = Callable[[ConnectionContext, PlatformAdapter], None]
Work = Callable[[PlatformAdapter, ConnectionContext, dict[str, Any] | None], T]


@dataclass(frozen=True)
class Resolved:
    org_id: uuid.UUID
    user_id: uuid.UUID
    connection_id: uuid.UUID
    context: ConnectionContext
    custom_prompt: str
    secret: dict[str, Any] | None
    credential_id: uuid.UUID | None
    credential_provider: str | None


@dataclass(frozen=True)
class Outcome:
    context: ConnectionContext
    adapter: PlatformAdapter
    custom_prompt: str
    value: Any


class Pipeline:
    def __init__(self, *, db: Database, secrets: SecretBox, registry: AdapterRegistry,
                 pools: PlatformPools, timeout_seconds: float,
                 entra: EntraTokens | None = None,
                 token_providers: dict[str, Any] | None = None) -> None:
        self.db = db
        self.secrets = secrets
        self.registry = registry
        self.pools = pools
        self.timeout = timeout_seconds
        self.entra = entra
        # Sign-in providers whose stored login is a refresh token exchanged for
        # access tokens per call: credential.provider -> object with access_token().
        self.token_providers: dict[str, Any] = dict(token_providers or {})
        if entra is not None:
            self.token_providers.setdefault("entra", entra)

    async def run(self, tool: str, connection_name: str, work: Work[T], *,
                  check: Check | None = None, statement: str | None = None,
                  user_id: uuid.UUID | None = None, allow_needs_signin: bool = False,
                  token_scopes: tuple[str, ...] | None = None) -> Outcome:
        # 1: Claude's verified caller -- or, for the dashboard's "Test connection",
        # the signed-in browser user, passed in explicitly by that page.
        person = user_id if user_id is not None else current_caller().user_id
        started = time.perf_counter()
        resolved = await asyncio.to_thread(                                   # 2
            self._resolve, person, connection_name, allow_needs_signin
        )
        ctx = resolved.context

        def audit(outcome: str, *, rows: int | None = None, error: str | None = None) -> None:
            record(self.db, org_id=resolved.org_id, user_id=resolved.user_id, tool=tool,
                   outcome=outcome, duration_ms=int((time.perf_counter() - started) * 1000),
                   connection_id=resolved.connection_id, platform=ctx.platform,
                   statement=statement, row_count=rows, error=error)

        try:
            adapter = self.registry.get(ctx.platform)
        except UnknownPlatform:
            raise ToolError(
                f"The connection '{ctx.name}' uses {ctx.platform}, which this server "
                "does not offer right now."
            ) from None

        try:
            if check is not None:
                check(ctx, adapter)                                           # 3
            secret = await asyncio.to_thread(                                 # 4
                self._platform_secret, resolved, adapter, token_scopes)
            value = await self.pools.run(                                     # 5
                ctx.platform, str(person),
                lambda: work(adapter, ctx, secret), timeout=self.timeout,
            )
        except QueryRejected as error:
            await asyncio.to_thread(audit, "rejected", error=str(error))
            raise ToolError(f"Query rejected: {error}") from None
        except CredentialRejected as error:
            await asyncio.to_thread(self._mark_needs_signin, resolved.credential_id)
            await asyncio.to_thread(audit, "error", error=str(error))
            raise ToolError(
                f"{error} The connection '{ctx.name}' now needs you to sign in again."
            ) from None
        except (AdapterError, Busy, CallTimedOut) as error:
            await asyncio.to_thread(audit, "error", error=str(error))
            raise ToolError(str(error)) from None
        except Exception as error:
            reference = REQUEST_ID.get()
            log.exception("Tool %s failed on %s", tool, ctx.platform)
            await asyncio.to_thread(audit, "error", error=f"{type(error).__name__}: {error}")
            raise ToolError(
                f"The {ctx.platform} call failed unexpectedly (reference {reference}). "
                "Try again; if it keeps failing, tell your administrator the reference."
            ) from None

        rows = getattr(value, "rows", None)
        await asyncio.to_thread(audit, "ok", rows=len(rows) if rows is not None else None)
        return Outcome(ctx, adapter, resolved.custom_prompt, value)

    # --- step 2: the connection (and its decrypted login) ------------------------

    def _resolve(self, user_id: uuid.UUID, connection_name: str,
                 allow_needs_signin: bool = False) -> Resolved:
        with self.db.session() as session:
            user = session.get(User, user_id)
            if user is None:
                raise ToolError("Your account was removed. Reconnect the connector in Claude.")
            try:
                row = connections.find_by_name(session, user_id, connection_name)
            except connections.ConnectionNotFound:
                names = [c.name for c in connections.list_for_user(session, user_id)]
                listed = ", ".join(f"'{n}'" for n in names) or "none yet"
                raise ToolError(
                    f"You have no connection named '{connection_name}'. Your connections: "
                    f"{listed}. Call list_connections for details."
                ) from None
            if row.status != "active" and not allow_needs_signin:
                raise ToolError(
                    f"The connection '{row.name}' needs you to sign in to {row.platform} again. "
                    "Open the IQ settings app to reconnect it."
                )
            secret = None
            credential = row.credential
            if credential is not None:
                try:
                    secret = read_secret(self.secrets, credential)
                except DecryptionError:
                    log.error("Stored login for connection %s cannot be decrypted", row.id)
                    raise ToolError(
                        f"The stored login for '{row.name}' can't be read. Reconnect it in "
                        "the IQ settings app."
                    ) from None
            return Resolved(
                user.org_id, user.id, row.id, connections.context_of(row), row.custom_prompt,
                secret, credential.id if credential else None,
                credential.provider if credential else None,
            )

    # --- step 4: what the adapter actually receives -------------------------------

    def _platform_secret(self, resolved: Resolved, adapter: PlatformAdapter,
                         scopes: tuple[str, ...] | None = None) -> dict[str, Any] | None:
        provider_name = resolved.credential_provider
        if provider_name is None or provider_name not in {"entra", "databricks"}:
            return resolved.secret
        provider = self.token_providers.get(provider_name)
        if provider is None:
            raise AdapterError(
                f"This server is not set up for {provider_name} sign-in any more."
            )
        assert resolved.secret is not None and resolved.credential_id is not None
        refresh_token = resolved.secret["refresh_token"]
        tokens: dict[str, str] = {}
        # Only the resources this call needs: a DAX call asks for Power BI, a SQL
        # call never does -- so a missing Power BI permission can't break SQL.
        if scopes is None:
            by_provider = getattr(adapter, "token_scopes_by_provider", {})
            scopes = by_provider.get(provider_name) or getattr(adapter, "token_scopes", ())
        for scope in scopes:
            minted = provider.access_token(
                credential_id=str(resolved.credential_id),
                version=resolved.context.credential_version or 0,
                refresh_token=refresh_token, scope=scope,
            )
            tokens[scope] = minted.token
            if minted.new_refresh_token:
                refresh_token = minted.new_refresh_token
                self._store_refresh_token(resolved, refresh_token)
        # The adapter sees access tokens only, never the refresh token.
        return {"access_tokens": tokens}

    def _store_refresh_token(self, resolved: Resolved, refresh_token: str) -> None:
        with self.db.session() as session:
            credential = session.get(Credential, resolved.credential_id)
            if credential is not None:
                refresh_secret(session, self.secrets, credential,
                               {**(resolved.secret or {}), "refresh_token": refresh_token})

    def _mark_needs_signin(self, credential_id: uuid.UUID | None) -> None:
        if credential_id is None:
            return
        with self.db.session() as session:
            session.execute(
                update(DataConnection)
                .where(DataConnection.credential_id == credential_id)
                .values(status="needs_signin")
            )
        log.warning("Connections using credential %s now need sign-in", credential_id)

    async def audit_simple(self, tool: str, outcome: str, started: float) -> None:
        """Audit a call that touches no connection (list_connections)."""
        caller = current_caller()

        def write() -> None:
            with self.db.session() as session:
                user = session.get(User, caller.user_id)
                org_id = user.org_id if user else uuid.UUID(int=0)
            record(self.db, org_id=org_id, user_id=caller.user_id, tool=tool, outcome=outcome,
                   duration_ms=int((time.perf_counter() - started) * 1000))

        await asyncio.to_thread(write)
