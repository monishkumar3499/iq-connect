"""
A short-lived, per-copy cache for catalogue metadata.

`data_map` then `profile_tables` in one conversation both need the same
metadata; without this they would each pay for it. Keyed by connection id,
credential version AND the exact scope, so an edited scope or a new login never
sees stale metadata. Losing it (restart, another copy) only costs speed.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict

from ..adapters.base import CatalogMeta, ConnectionContext

TTL_SECONDS = 300
MAX_ENTRIES = 500

_entries: OrderedDict[str, tuple[float, CatalogMeta]] = OrderedDict()
_lock = threading.Lock()


def _key(ctx: ConnectionContext) -> str:
    return json.dumps([ctx.id, ctx.credential_version, ctx.scope], sort_keys=True, default=str)


def get(ctx: ConnectionContext) -> CatalogMeta | None:
    with _lock:
        hit = _entries.get(_key(ctx))
        if hit is None or hit[0] < time.monotonic():
            return None
        return hit[1]


def put(ctx: ConnectionContext, meta: CatalogMeta) -> None:
    with _lock:
        _entries[_key(ctx)] = (time.monotonic() + TTL_SECONDS, meta)
        _entries.move_to_end(_key(ctx))
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)


def forget(ctx: ConnectionContext) -> None:
    with _lock:
        _entries.pop(_key(ctx), None)


def clear() -> None:
    with _lock:
        _entries.clear()

