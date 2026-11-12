"""
The dynamic registry: which platforms this copy of the service serves.

Every folder in `backend/platforms/` whose `tools` package defines `PLATFORM` (a
`PlatformPlugin`) is a platform: its adapter (the common tools), its setup driver, its
sign-in token providers and how it describes itself. Adding a platform means adding a
folder -- nothing in core names one.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.core.configuration.settings import Settings
from backend.core.dynamic_registry.base import PlatformAdapter


class UnknownPlatform(Exception):
    pass


@dataclass(frozen=True)
class PlatformPlugin:
    key: str
    label: str
    order: int
    # The platform's implementation of the common tools.
    make_adapter: Callable[[Settings], PlatformAdapter]
    # The guided setup's driver: (app state, user id) -> PlatformSetup.
    setup: Callable[[Any, Any], Any]
    # (app state) -> PlatformStatus, for the platform tiles.
    status: Callable[[Any], Any]
    # Credential.provider -> token provider, for sign-ins this platform owns.
    make_token_providers: Callable[[Settings], dict[str, Any]] = field(
        default=lambda settings: {})
    enabled: Callable[[Settings], bool] = field(default=lambda settings: True)
    # (app state, the person's PlatformAccount or None) -> the Microsoft scopes a Microsoft
    # sign-in for this platform asks consent for; [] = this platform has no Microsoft sign-in.
    microsoft_scopes: Callable[[Any, Any], list[str]] = field(
        default=lambda state, account: [])


def discover() -> list[PlatformPlugin]:
    """Every platform folder under backend/platforms, in display order."""
    import backend.platforms as package

    found = []
    for info in pkgutil.iter_modules(package.__path__):
        if not info.ispkg:
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}.tools")
        plugin = getattr(module, "PLATFORM", None)
        if isinstance(plugin, PlatformPlugin):
            found.append(plugin)
    return sorted(found, key=lambda p: p.order)


class AdapterRegistry:
    def __init__(self, adapters: list[PlatformAdapter]) -> None:
        self._adapters = {adapter.platform: adapter for adapter in adapters}

    def get(self, platform: str) -> PlatformAdapter:
        if platform not in self._adapters:
            raise UnknownPlatform(platform)
        return self._adapters[platform]

    @property
    def platforms(self) -> list[str]:
        return sorted(self._adapters)

    def close(self) -> None:
        """At shutdown: close any connections adapters keep open."""
        for adapter in self._adapters.values():
            close = getattr(adapter, "close", None)
            if close is not None:
                close()


class Platforms:
    """The enabled platforms, their adapters and their token providers."""

    def __init__(self, settings: Settings, plugins: list[PlatformPlugin] | None = None) -> None:
        self._plugins = [p for p in (plugins if plugins is not None else discover())
                         if p.enabled(settings)]
        self.registry = AdapterRegistry([p.make_adapter(settings) for p in self._plugins])
        self.token_providers: dict[str, Any] = {}
        for plugin in self._plugins:
            self.token_providers.update(plugin.make_token_providers(settings))

    def plugins(self) -> list[PlatformPlugin]:
        return list(self._plugins)

    def plugin(self, key: str) -> PlatformPlugin | None:
        return next((p for p in self._plugins if p.key == key), None)


def default_registry(settings: Settings) -> AdapterRegistry:
    """The adapters of every enabled platform (tests and tools that need only those)."""
    return Platforms(settings).registry
