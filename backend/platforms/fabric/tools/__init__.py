"""
Microsoft Fabric: warehouses (T-SQL over ODBC) and semantic models (DAX over Power BI).

  query.py      the common tools (list, describe, run, catalog) and DAX
  discovery.py  the guided setup: sign in, workspaces, warehouses, models
"""

from __future__ import annotations

from typing import Any

from backend.core.configuration.settings import Settings
from backend.core.connection_manager.drivers import PlatformStatus
from backend.core.dynamic_registry.registry import PlatformPlugin
from backend.platforms.fabric.auth import (
    FABRIC_API_SCOPE,
    SERVICE_PRINCIPAL_PROVIDER,
    ServicePrincipalTokens,
)
from backend.platforms.fabric.client import FabricConnectionPool
from backend.platforms.fabric.tools.discovery import FabricSetup
from backend.platforms.fabric.tools.query import FabricAdapter


def _adapter(settings: Settings) -> FabricAdapter:
    return FabricAdapter(
        login_timeout=settings.platform_login_timeout_seconds,
        query_timeout=int(settings.query_timeout_seconds),
        pool=FabricConnectionPool(idle_seconds=settings.fabric_connection_idle_seconds,
                                  max_age_seconds=settings.fabric_connection_max_age_seconds),
    )


def _status(state: Any) -> PlatformStatus:
    how = ("Microsoft single sign-on or a service principal" if state.entra.ready()
           else "A service principal (Microsoft sign-in isn't set up here yet)")
    return PlatformStatus("fabric", "Microsoft Fabric", True, how)


PLATFORM = PlatformPlugin(
    key="fabric", label="Microsoft Fabric", order=10,
    make_adapter=_adapter, setup=FabricSetup, status=_status,
    make_token_providers=lambda settings: {SERVICE_PRINCIPAL_PROVIDER: ServicePrincipalTokens()},
    microsoft_scopes=lambda state, account: [FABRIC_API_SCOPE],
)
