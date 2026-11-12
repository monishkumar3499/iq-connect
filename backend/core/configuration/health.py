"""
Operations endpoints for load balancers and container platforms.

/healthz  -- the process is alive. Never touches dependencies, so a slow
             database does not get healthy copies restarted.
/readyz   -- this copy can serve traffic: every registered dependency check
             (database, Key Vault, ...) passed. Failing copies are taken out of
             the load balancer instead of answering users with errors.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

ReadinessCheck = Callable[[], Awaitable[None]]

# Load balancers give up after a few seconds; a check that takes longer than
# this is treated as failed rather than left to hang the probe.
CHECK_TIMEOUT_SECONDS = 2.0

log = logging.getLogger(__name__)
router = APIRouter(tags=["operations"])


def add_readiness_check(app: FastAPI, name: str, check: ReadinessCheck) -> None:
    """Register a dependency check. It passes by returning, fails by raising."""
    app.state.readiness_checks[name] = check


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    checks: dict[str, ReadinessCheck] = request.app.state.readiness_checks
    results: dict[str, str] = {}
    for name, check in checks.items():
        try:
            await asyncio.wait_for(check(), CHECK_TIMEOUT_SECONDS)
            results[name] = "ok"
        except Exception:
            # The details (hostnames, driver messages) go to the log only;
            # /readyz is often reachable by more people than the logs are.
            log.exception("Readiness check %r failed", name)
            results[name] = "failed"
    ready = all(result == "ok" for result in results.values())
    return JSONResponse(
        {"status": "ready" if ready else "not ready", "checks": results},
        status_code=200 if ready else 503,
    )
