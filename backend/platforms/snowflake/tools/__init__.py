"""
Snowflake: the SQL API with the person's own OAuth token, in one pinned role, on their own
Snowflake accounts.

  query.py      the common tools (list, describe, run, query ids, catalog)
  discovery.py  the guided setup: sign in, accounts, warehouses, databases, schemas
"""

from __future__ import annotations

from typing import Any

from backend.core.configuration.settings import Settings
from backend.core.connection_manager.drivers import PlatformStatus
from backend.core.dynamic_registry.registry import PlatformPlugin
from backend.platforms.snowflake.auth import SnowflakeOAuth, account_url
from backend.platforms.snowflake.tools.discovery import SnowflakeSetup
from backend.platforms.snowflake.tools.query import SnowflakeAdapter


def _adapter(settings: Settings) -> SnowflakeAdapter:
    return SnowflakeAdapter(
        base_url=account_url(settings.snowflake_account) if settings.snowflake_account else "",
        role=settings.snowflake_role, entra_scope=settings.snowflake_entra_scope,
        query_timeout=int(settings.query_timeout_seconds))


def _status(state: Any) -> PlatformStatus:
    how = ("Microsoft single sign-on (External OAuth) or Snowflake sign-in"
           if state.entra.ready() else "Snowflake sign-in")
    return PlatformStatus("snowflake", "Snowflake", True, how)


def _microsoft_scopes(state: Any, account: Any) -> list[str]:
    """External OAuth: exactly the scope of the account's one role."""
    own = account.options.get("entra_scope") if account is not None else ""
    scope = own or state.settings.snowflake_entra_scope
    return [scope] if scope else []


PLATFORM = PlatformPlugin(
    key="snowflake", label="Snowflake", order=30,
    make_adapter=_adapter, setup=SnowflakeSetup, status=_status,
    make_token_providers=lambda settings: {"snowflake": SnowflakeOAuth(settings)},
    microsoft_scopes=_microsoft_scopes,
)
