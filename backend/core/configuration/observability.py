"""Logging setup and the request-arrival trace."""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")

# Polled every few seconds by load balancers; tracing them buries real traffic.
QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})

# A caller-supplied id is echoed only if it looks like an id, so nobody can
# inject newlines or fake log lines through the header.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# HTTP client libraries log every full URL they call at INFO, query string
# included. Our outbound calls (Entra, platform APIs, CIMD documents) can carry
# codes and tokens there, so these loggers only speak up for warnings.
_URL_LOGGING_LIBRARIES = ("httpx", "httpx2", "httpcore", "httpcore2")

log = logging.getLogger("backend.request")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID.get()
        return True


def configure_logging(level: str) -> None:
    """Send all logs to stderr with the request id. Safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for name in _URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
    if any(getattr(handler, "_iq_connect", False) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s")
    )
    handler.addFilter(_RequestIdFilter())
    handler._iq_connect = True  # type: ignore[attr-defined]
    root.addHandler(handler)


class RequestTraceMiddleware:
    """Gives every request an id (`X-Request-ID`) and logs its arrival and end."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex[:16]
        token = REQUEST_ID.set(request_id)

        # Path only, never the query string: OAuth redirects carry one-time
        # codes and state values there, and logs are read by more people
        # than should hold those.
        method, path = scope["method"], scope["path"]
        quiet = path in QUIET_PATHS
        status = 0

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        if not quiet:
            log.info(">> %s %s", method, path)
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            elapsed = time.perf_counter() - started
            log.exception("<< %s %s -> crashed in %.2fs", method, path, elapsed)
            raise
        else:
            if not quiet:
                elapsed = time.perf_counter() - started
                log.info("<< %s %s -> %s in %.2fs", method, path, status, elapsed)
        finally:
            REQUEST_ID.reset(token)
