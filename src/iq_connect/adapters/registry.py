"""Which platforms this copy of the service can serve."""

from __future__ import annotations

from ..settings import Settings
from .base import PlatformAdapter
from .databricks.adapter import DatabricksAdapter
from .demo import DemoAdapter
from .fabric.adapter import FabricAdapter
from .fabric.pool import FabricConnectionPool


class UnknownPlatform(Exception):
    pass


class AdapterRegistry:
    def __init__(self, adapters: list[PlatformAdapter]) -> None:
        self._adapters = {adapter.platform: adapter for adapter in adapters}

    def get(self, platform: str) -> PlatformAdapter:
        try:
            return self._adapters[platform]
        except KeyError:
            raise UnknownPlatform(platform) from None

    @property
    def platforms(self) -> list[str]:
        return sorted(self._adapters)

    def close(self) -> None:
        """At shutdown: close any connections adapters keep open."""
        for adapter in self._adapters.values():
            close = getattr(adapter, "close", None)
            if close is not None:
                close()


def default_registry(settings: Settings) -> AdapterRegistry:
    adapters: list[PlatformAdapter] = []
    if settings.demo_enabled:
        adapters.append(DemoAdapter())
    if settings.entra_configured:
        # Fabric works only through a linked Microsoft account.
        adapters.append(FabricAdapter(
            login_timeout=settings.platform_login_timeout_seconds,
            query_timeout=int(settings.query_timeout_seconds),
            pool=FabricConnectionPool(
                idle_seconds=settings.fabric_connection_idle_seconds,
                max_age_seconds=settings.fabric_connection_max_age_seconds,
            ),
        ))
    if settings.databricks_enabled:
        adapters.append(DatabricksAdapter(
            host=settings.databricks_host,
            query_timeout=int(settings.query_timeout_seconds),
        ))
    # Snowflake is added by its own roadmap step.
    return AdapterRegistry(adapters)
