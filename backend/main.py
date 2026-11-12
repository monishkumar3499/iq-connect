"""
Builds the web application. Every router is wired here and nowhere else.

There is deliberately no module-level `app`: importing this module must not
read `.env` or configure logging, so tests can build apps with their own
settings. Uvicorn calls `create_app` as a factory (see `__main__.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from backend import __version__
from backend.core import web
from backend.core.auth import sign_in as identity_routes
from backend.core.auth.browser_sessions import BrowserSessions
from backend.core.auth.identity import make_identity_provider
from backend.core.auth.mcp_gate import McpGate
from backend.core.auth.microsoft import EntraTokens
from backend.core.auth.oauth import authorize, clients, metadata, token
from backend.core.auth.oauth.common import OAuthError, oauth_error_response
from backend.core.configuration import health as ops
from backend.core.configuration.app_registrations import MicrosoftApps, setup_code
from backend.core.configuration.observability import RequestTraceMiddleware, configure_logging
from backend.core.configuration.settings import Settings, get_settings
from backend.core.connection_manager import platform_sign_in as credential_routes
from backend.core.dynamic_registry.pipeline import Pipeline
from backend.core.dynamic_registry.pools import PlatformPools
from backend.core.dynamic_registry.registry import Platforms
from backend.core.dynamic_registry.render import QueryHandles
from backend.core.dynamic_registry.tools import build_mcp_server
from backend.core.security.crypto import SecretBox, make_key_encryption_key, make_signing_keys
from backend.core.security.tokens import AccessTokens
from backend.platforms.fabric.client import FabricApi
from backend.shared.database import Database, safe_url

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
    # The Microsoft app registration: from .env, or entered once in the setup UI.
    microsoft = MicrosoftApps(settings, database, secret_box)
    entra = EntraTokens(settings, microsoft)
    # Every platform folder under backend/platforms: its adapter and its token providers.
    platforms = Platforms(settings)
    pipeline = Pipeline(
        db=database, secrets=secret_box, registry=platforms.registry,
        pools=pools, timeout_seconds=settings.query_timeout_seconds, entra=entra,
        token_providers=platforms.token_providers,
    )
    mcp_server = build_mcp_server(settings, pipeline, QueryHandles(signing_keys.all_keys()))
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
    app.state.microsoft = microsoft
    # The operator's code for entering the Microsoft app in the UI (see microsoft_app.py).
    app.state.setup_code = setup_code(signing_keys.all_keys()[-1])
    app.state.platforms = platforms
    # The platforms' own sign-ins, by name, for the sign-in routes and setup drivers.
    app.state.databricks_oauth = platforms.token_providers.get("databricks")
    app.state.snowflake_oauth = platforms.token_providers.get("snowflake")
    app.state.fabric_sp = platforms.token_providers.get("fabric_sp")
    # Replaceable so tests can stand in for the Fabric REST API.
    app.state.fabric_api = FabricApi
    app.state.sessions = BrowserSessions(
        signing_keys.all_keys(),
        secure=settings.secure_cookies,
        max_age_seconds=settings.session_hours * 3600,
    )
    app.state.identity = make_identity_provider(settings, signing_keys.all_keys(), microsoft)
    # Replaceable so tests can serve Client ID Metadata Documents locally.
    app.state.cimd_fetch = clients.fetch_cimd_document
    app.state.readiness_checks = {}

    app.add_middleware(RequestTraceMiddleware)
    app.add_exception_handler(OAuthError, lambda _, error: oauth_error_response(error))
    app.include_router(ops.router)
    for module in (metadata, clients, authorize, token, identity_routes, credential_routes):
        app.include_router(module.router)
    web.install(app)

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
    _announce_microsoft_setup(settings, microsoft, app.state.setup_code)
    return app


def _announce_microsoft_setup(settings: Settings, microsoft: MicrosoftApps, code: str) -> None:
    """
    Until a Microsoft app exists, tell the operator -- in the server window only --
    where to enter it and the code that proves they run this server.
    """
    try:
        ready = microsoft.ready()
    except Exception:  # noqa: BLE001 - e.g. the database isn't migrated yet
        return
    if not ready and settings.requires_setup_code:
        log.warning("Microsoft (Entra) isn't set up yet. Open %s/app/setup/microsoft and "
                    "enter setup code %s", settings.public_base_url, code)
    elif not ready:
        log.warning("Microsoft (Entra) isn't set up yet. Open %s/app/setup/microsoft to "
                    "enter its client ID and secret", settings.public_base_url)


# --- command line ----------------------------------------------------------------------
# Command line:
#
#     python -m backend.main                                  run the service
#     python -m backend.main create-keys                      once per server: the key files
#                                                           (never overwrites existing ones)
#     python -m backend.main migrate                          create / update the database tables
#     python -m backend.main new-migration "what changed"     after editing shared/models.py (run it
#                                                           against a scratch database)
#     python -m backend.main microsoft-setup-code             the code for entering the Microsoft
#                                                           app registration in the setup UI
#     python -m backend.main check-config                     the settings, and whether Microsoft,
#                                                           Fabric, Databricks, Snowflake answer
#     python -m backend.main check-connections EMAIL          that person's real connections,
#                                                           through Claude's own call path

def serve() -> None:
    settings = get_settings()
    uvicorn.run(
        "backend.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        # Our own logging setup and request trace replace uvicorn's, which
        # would otherwise log every request a second time.
        log_config=None,
        access_log=False,
    )


def create_keys() -> int:
    """
    Create the local encryption and signing key files if missing. Outside APP_ENV=dev the
    service never does this by itself: a fresh key would orphan every stored credential.
    """
    from pathlib import Path

    from backend.core.security.crypto import LocalKeyring

    settings = get_settings()
    for label, name in (("encryption", settings.local_key_file),
                        ("signing", settings.local_signing_key_file)):
        path = Path(name)
        existed = path.exists()
        LocalKeyring.load(path, create=True)
        print(f"{label} key file {path}: {'kept (already there)' if existed else 'created'}")
    print("Keep these files safe and backed up; losing them loses every stored sign-in.")
    return 0


def migrate(message: str | None = None) -> int:
    """Upgrade the database to the newest schema, or write a new migration from the models."""
    from alembic import command

    from backend.shared.database import migration_config

    config = migration_config(get_settings().database_url)
    if message:
        command.revision(config, message=message, autogenerate=True)
    else:
        command.upgrade(config, "head")
    return 0


def print_setup_code() -> int:
    """Only someone who can run commands on the server can see this code."""
    from backend.core.configuration.app_registrations import setup_code
    from backend.core.security.crypto import make_signing_keys

    settings = get_settings()
    keys = make_signing_keys(settings)
    print(f"Microsoft setup code: {setup_code(keys.all_keys()[-1])}")
    print(f"Enter it at {settings.public_base_url}/app/setup/microsoft")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.main")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("create-keys", help="create the key files once (never overwrites)")
    commands.add_parser("migrate", help="create or update the database tables")
    new = commands.add_parser("new-migration", help="write a migration after editing the models")
    new.add_argument("message")
    commands.add_parser("check-config", help="check the settings and reach every service")
    check = commands.add_parser("check-connections",
                                help="check one person's connections against the real platforms")
    check.add_argument("email")
    check.add_argument("--with-query", action="store_true",
                       help="also run SELECT 1 (may resume a suspended warehouse)")
    commands.add_parser("microsoft-setup-code",
                        help="print the code for entering the Microsoft app in the setup UI")
    args = parser.parse_args(argv)

    if args.command == "create-keys":
        return create_keys()
    if args.command == "migrate":
        return migrate()
    if args.command == "new-migration":
        return migrate(args.message)
    if args.command == "microsoft-setup-code":
        return print_setup_code()
    if args.command == "check-config":
        from backend.core.configuration.checks import check_config

        return check_config(get_settings())
    if args.command == "check-connections":
        from backend.core.configuration.checks import check_connections

        return check_connections(get_settings(), args.email, args.with_query)
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
