"""
Reusing Fabric warehouse connections between queries.

Opening one costs a TLS handshake plus Entra authentication at the SQL
endpoint, which a conversation of many short queries would otherwise pay
every time. The rules that make reuse safe here:

  * A connection is only reused for the same connection id AND the same access
    token it was opened with (the key holds a fingerprint of the token, never
    the token). A new token -- hourly, or after relinking -- means a new
    connection, so access revoked in Entra is not kept alive by an old socket.
  * No connection lives longer than `max_age` (below an access token's
    lifetime) or sits idle longer than `idle`.
  * A connection serves one query at a time: it is taken out of the pool while
    in use, and a second simultaneous query opens its own.
  * A connection that raised is closed, never returned.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

Key = tuple[str, str, str, str]  # connection id, endpoint, database, token fingerprint


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:24]


@dataclass
class _Idle:
    connection: Any
    opened: float
    returned: float = field(default=0.0)


class FabricConnectionPool:
    def __init__(self, *, idle_seconds: float, max_age_seconds: float,
                 max_idle_per_key: int = 2, max_idle_total: int = 200,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._idle_seconds = idle_seconds
        self._max_age = max_age_seconds
        self._per_key = max_idle_per_key
        self._total = max_idle_total
        self._clock = clock
        self._idle: dict[Key, list[_Idle]] = {}
        self._lock = threading.Lock()

    def now(self) -> float:
        """The pool's clock, used to stamp when a connection was opened."""
        return self._clock()

    def take(self, key: Key) -> tuple[Any, float] | None:
        """An idle, still-young connection for this key, or None."""
        expired: list[Any] = []
        try:
            with self._lock:
                self._sweep(expired)
                stack = self._idle.get(key)
                if not stack:
                    return None
                entry = stack.pop()
                if not stack:
                    del self._idle[key]
                return entry.connection, entry.opened
        finally:
            _close_all(expired)

    def give_back(self, key: Key, connection: Any, opened: float) -> None:
        """Return a healthy connection after use (it may be closed instead)."""
        now = self._clock()
        keep = now - opened < self._max_age
        surplus: list[Any] = []
        with self._lock:
            stack = self._idle.setdefault(key, [])
            if keep and len(stack) < self._per_key:
                stack.append(_Idle(connection, opened, now))
                while sum(len(s) for s in self._idle.values()) > self._total:
                    surplus.append(self._evict_oldest())
            else:
                surplus.append(connection)
                if not stack:
                    del self._idle[key]
        _close_all(surplus)

    def close_all(self) -> None:
        with self._lock:
            everything = [e.connection for stack in self._idle.values() for e in stack]
            self._idle.clear()
        _close_all(everything)

    def idle_count(self) -> int:
        with self._lock:
            return sum(len(stack) for stack in self._idle.values())

    # Caller holds the lock for both helpers below.

    def _sweep(self, expired: list[Any]) -> None:
        now = self._clock()
        for key in list(self._idle):
            fresh = []
            for entry in self._idle[key]:
                too_idle = now - entry.returned >= self._idle_seconds
                too_old = now - entry.opened >= self._max_age
                if too_idle or too_old:
                    expired.append(entry.connection)
                else:
                    fresh.append(entry)
            if fresh:
                self._idle[key] = fresh
            else:
                del self._idle[key]

    def _evict_oldest(self) -> Any:
        key, index = min(
            ((k, i) for k, stack in self._idle.items() for i in range(len(stack))),
            key=lambda ki: self._idle[ki[0]][ki[1]].returned,
        )
        entry = self._idle[key].pop(index)
        if not self._idle[key]:
            del self._idle[key]
        return entry.connection


def _close_all(connections: list[Any]) -> None:
    for connection in connections:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing a dead connection
            log.debug("Closing a pooled Fabric connection failed", exc_info=True)
