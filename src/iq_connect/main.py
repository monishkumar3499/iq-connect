"""
Builds the web application. Every router is wired here and nowhere else.

There is deliberately no module-level `app`: importing this module must not
read `.env` or configure logging, so tests can build apps with their own
settings. Uvicorn calls `create_app` as a factory (see `__main__.py`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from . import __version__, ops
from .adapters.fabric.api import FabricApi
from .adapters.registry import default_registry
from .credentials import databricks_routes
from .credentials import routes as credential_routes
from .credentials.providers.databricks import DatabricksOAuth
from .credentials.providers.entra import EntraTokens
from .db.session import Database, safe_url
from .execution.pools import PlatformPools
from .identity import routes as identity_routes
from .identity.providers import make_identity_provider
from .mcp.bearer import McpGate
from .mcp.pipeline import Pipeline
from .mcp.server import build_mcp_server
from .oauth import authorize, clients, metadata, token
from .oauth.common import OAuthError, oauth_error_response
from .observability.logging import RequestTraceMiddleware, configure_logging
from .security.crypto import SecretBox
from .security.keys import make_key_encryption_key, make_signing_keys
from .security.tokens import AccessTokens
from .security.web_session import BrowserSessions
from .settings import Settings, get_settings
from .web import connect, dashboard, wizard

log = logging.getLogger(__name__)


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """
    Which Host headers /mcp answers to. The SDK's default accepts only
    127.0.0.1/localhost, which would refuse every request arriving through a
    tunnel or a real domain -- so the public address is always listed here.
    """
    public = urlsplit(settings.public_base_url)
    hosts = [public.netloc]
    origins = [f"{public.scheme}://{public.netloc}"]
    if settings.is_dev:
        hosts += ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        origins += ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    database = Database(settings.database_url)
    secret_box = SecretBox(make_key_encryption_key(settings))
    signing_keys = make_signing_keys(settings)
    access_tokens = AccessTokens(
        signing_keys,
        issuer=settings.issuer,
        lifetime=timedelta(minutes=settings.access_token_minutes),
    )
    pools = PlatformPools(
        threads_per_platform=settings.platform_threads,
        calls_per_person=settings.calls_per_person,
        wait_seconds=settings.busy_wait_seconds,
    )
    entra = EntraTokens(settings) if settings.entra_configured else None
    databricks_oauth = DatabricksOAuth(settings) if settings.databricks_configured else None
    pipeline = Pipeline(
        db=database, secrets=secret_box, registry=default_registry(settings),
        pools=pools, timeout_seconds=settings.query_timeout_seconds, entra=entra,
        token_providers={"databricks": databricks_oauth} if databricks_oauth else None,
    )
    mcp_server = build_mcp_server(settings, pipeline)
    # Stateless + plain JSON responses: any copy of the service can answer any
    # request, and nothing about a conversation lives in this process.
    mcp_server.streamable_http_app(
        stateless_http=True, json_response=True,
        transport_security=_transport_security(settings),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp_server.session_manager.run():
            yield
        pools.shutdown()
        pipeline.registry.close()
        database.dispose()

    # The interactive API docs list every route; useful while building,
    # needless exposure in production.
    app = FastAPI(
        title="IQ Connect",
        version=__version__,
        docs_url="/docs" if settings.is_dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_dev else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = database
    app.state.secrets = secret_box
    app.state.access_tokens = access_tokens
    app.state.pipeline = pipeline
    app.state.entra = entra
    app.state.databricks_oauth = databricks_oauth
    # Replaceable so tests can stand in for the Fabric REST API.
    app.state.fabric_api = FabricApi
    app.state.sessions = BrowserSessions(
        signing_keys.all_keys(),
        secure=settings.secure_cookies,
        max_age_seconds=settings.session_hours * 3600,
    )
    app.state.identity = make_identity_provider(settings, signing_keys.all_keys())
    # Replaceable so tests can serve Client ID Metadata Documents locally.
    app.state.cimd_fetch = clients.fetch_cimd_document
    app.state.readiness_checks = {}

    app.add_middleware(RequestTraceMiddleware)
    app.add_exception_handler(OAuthError, lambda _, error: oauth_error_response(error))
    app.include_router(ops.router)
    for module in (metadata, clients, authorize, token, identity_routes, credential_routes,
                   databricks_routes,
                   connect, dashboard, wizard):
        app.include_router(module.router)

    gate = McpGate(
        mcp_server.session_manager.handle_request,
        access_tokens,
        audience=settings.mcp_url,
        metadata_url=f"{settings.public_base_url}/.well-known/oauth-protected-resource/mcp",
    )
    app.router.routes.append(Route("/mcp", endpoint=gate, methods=["GET", "POST", "DELETE"]))

    async def database_reachable() -> None:
        await asyncio.to_thread(database.check)

    async def keys_usable() -> None:
        # A thread too: with Key Vault this becomes a network call.
        await asyncio.to_thread(secret_box.check)

    ops.add_readiness_check(app, "database", database_reachable)
    ops.add_readiness_check(app, "keys", keys_usable)

    log.info(
        "IQ Connect %s ready (APP_ENV=%s). Connector URL: %s. Database: %s",
        __version__, settings.app_env, settings.mcp_url, safe_url(settings.database_url),
    )
    return app
