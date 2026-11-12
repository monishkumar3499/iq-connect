"""
Databricks: Unity Catalog browsing and the SQL Statement Execution API, on the person's
own workspaces.

  query.py      the common tools (list, describe, run, query ids, catalog)
  discovery.py  the guided setup: sign in, workspaces, warehouses, catalogs, schemas
"""

from __future__ import annotations

from typing import Any

from backend.core.configuration.settings import Settings
from backend.core.connection_manager.drivers import PlatformStatus
from backend.core.dynamic_registry.registry import PlatformPlugin
from backend.platforms.databricks.auth import AZURE_DATABRICKS_SCOPE, DatabricksOAuth
from backend.platforms.databricks.tools.discovery import DatabricksSetup
from backend.platforms.databricks.tools.query import DatabricksAdapter


def _adapter(settings: Settings) -> DatabricksAdapter:
    return DatabricksAdapter(host=settings.databricks_host,
                             query_timeout=int(settings.query_timeout_seconds))


def _status(state: Any) -> PlatformStatus:
    how = ("Microsoft single sign-on (Azure) or Databricks sign-in" if state.entra.ready()
           else "Databricks sign-in")
    return PlatformStatus("databricks", "Databricks", True, how)


PLATFORM = PlatformPlugin(
    key="databricks", label="Databricks", order=20,
    make_adapter=_adapter, setup=DatabricksSetup, status=_status,
    make_token_providers=lambda settings: {"databricks": DatabricksOAuth(settings)},
    microsoft_scopes=lambda state, account: [AZURE_DATABRICKS_SCOPE],
)
