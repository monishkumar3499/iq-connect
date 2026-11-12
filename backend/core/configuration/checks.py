"""
Checks an operator runs from the command line against the REAL services -- no fakes:

  check-config               what this server is set up for, and whether it can reach
                             Microsoft, Fabric, Power BI and the configured Databricks
                             workspace / Snowflake account. Never prints a secret.
  check-connections EMAIL    every connection of that person, through the same pipeline
                             Claude's calls use (sign-in, token exchange, policy, platform,
                             audit): lists its tables (warehouses) or runs EVALUATE ROW
                             (semantic models). --with-query also runs SELECT 1, which may
                             resume (and bill) a suspended warehouse.

Both print one line per check and return 0 only when everything passed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from backend.core.configuration.settings import Settings

OK, FAIL, INFO = "  ok  ", " FAIL ", "  --  "


def _line(mark: str, label: str, detail: str = "") -> None:
    print(f"[{mark}] {label}" + (f": {detail}" if detail else ""))


def _reachable(http: httpx.Client, label: str, url: str, expect: set[int]) -> bool:
    """Any of `expect` means the service answered (401 = up, wants a token)."""
    try:
        status = http.get(url).status_code
    except httpx.HTTPError as error:
        _line(FAIL, label, f"{type(error).__name__} reaching {url}")
        return False
    good = status in expect
    _line(OK if good else FAIL, label, f"HTTP {status} from {url}")
    return good


def check_config(settings: Settings) -> int:
    from backend.main import create_app

    print(f"IQ Connect configuration check (APP_ENV={settings.app_env})")
    passed = True
    app = create_app(settings)
    state = app.state
    _line(INFO, "Public address", settings.public_base_url)
    _line(INFO, "Connector URL for Claude", settings.mcp_url)
    try:
        state.db.check()
        _line(OK, "Database reachable")
    except Exception as error:  # noqa: BLE001 - reported, not raised
        passed = False
        _line(FAIL, "Database reachable", type(error).__name__)
    try:
        state.secrets.check()
        _line(OK, "Encryption keys usable")
    except Exception as error:  # noqa: BLE001
        passed = False
        _line(FAIL, "Encryption keys usable", type(error).__name__)

    microsoft = state.microsoft.status()
    if microsoft is None:
        passed = passed and settings.sso_provider != "entra"
        _line(FAIL if settings.sso_provider == "entra" else INFO, "Microsoft app",
              "not set up (ENTRA_CLIENT_ID / ENTRA_CLIENT_SECRET, or /app/setup/microsoft)")
    else:
        tenants = settings.allowed_tenants
        who = ("any company (multi-tenant)" if tenants is None and not microsoft.tenant
               else f"tenant {microsoft.tenant}" if microsoft.tenant
               else "tenants " + ", ".join(sorted(tenants or [])))
        _line(OK, "Microsoft app", f"client {microsoft.client_id} from {microsoft.source}, {who}")
        _line(INFO, "Redirect URIs to register (Web)",
              f"{settings.public_base_url}/auth/callback/entra , "
              f"{settings.public_base_url}/auth/platform/entra/callback")
    _line(INFO, "Sign-in to IQ Connect", settings.sso_provider)
    _line(INFO, "Admins (ADMIN_EMAILS)", ", ".join(sorted(settings.admins)) or "none listed")

    try:
        import pyodbc

        drivers = [d for d in pyodbc.drivers() if "ODBC Driver 18 for SQL Server" in d]
        _line(OK if drivers else FAIL, "ODBC Driver 18 for SQL Server (Fabric SQL)",
              "installed" if drivers else "missing")
        passed = passed and bool(drivers)
    except ImportError:
        passed = False
        _line(FAIL, "pyodbc (Fabric SQL)", "not installed")

    with httpx.Client(timeout=15.0, follow_redirects=False) as http:
        passed &= _reachable(http, "Microsoft sign-in (Entra)",
                             "https://login.microsoftonline.com/organizations/v2.0/"
                             ".well-known/openid-configuration", {200})
        passed &= _reachable(http, "Fabric REST API", "https://api.fabric.microsoft.com/v1/"
                             "workspaces", {401})
        passed &= _reachable(http, "Power BI REST API (DAX)",
                             "https://api.powerbi.com/v1.0/myorg/groups", {401, 403})
        if settings.databricks_host:
            passed &= _reachable(http, "Default Databricks workspace",
                                 f"{settings.databricks_host}/oidc/.well-known/"
                                 "oauth-authorization-server", {200})
        else:
            _line(INFO, "Default Databricks workspace", "none (people enter their own)")
        if settings.snowflake_account:
            passed &= _reachable(http, "Default Snowflake account",
                                 f"https://{settings.snowflake_account}.snowflakecomputing.com/"
                                 "api/v2/statements", {401, 405, 400, 404})
        else:
            _line(INFO, "Default Snowflake account", "none (people enter their own)")
    for plugin in state.platforms.plugins():
        status = plugin.status(state)
        _line(INFO, f"Platform {status.label}", status.how)
    print("All checks passed." if passed else "Some checks FAILED -- see above.")
    return 0 if passed else 1


async def _check_one(state: Any, user_id: Any, row: dict[str, Any], with_query: bool) -> bool:
    from mcp.server.mcpserver.exceptions import ToolError

    started = time.perf_counter()
    name = row["name"]
    try:
        if row["model"]:
            from backend.platforms.fabric.auth import POWERBI_SCOPE
            from backend.platforms.fabric.tools.discovery import PROBE_DAX

            model = row["model"]
            outcome = await state.pipeline.run(
                "check_connection", name, user_id=user_id, allow_needs_signin=True,
                token_scopes=(POWERBI_SCOPE,),
                work=lambda adapter, ctx, secret: adapter.execute_dax(ctx, secret, model,
                                                                      PROBE_DAX, 1))
            detail = f"semantic model answered EVALUATE ROW ({len(outcome.value.rows)} row)"
        else:
            outcome = await state.pipeline.run(
                "check_connection", name, user_id=user_id, allow_needs_signin=True,
                work=lambda adapter, ctx, secret: adapter.list_objects(ctx, secret, None))
            detail = f"{len(outcome.value)} tables/views readable in {', '.join(row['schemas'])}"
            if with_query:
                from backend.core.security.policy import check_query

                def probe(adapter, ctx, secret):
                    check_query("SELECT 1", adapter.dialect, ctx)
                    return adapter.run_query(ctx, secret, "SELECT 1", 1)

                await state.pipeline.run("check_connection", name, user_id=user_id,
                                         statement="SELECT 1", work=probe)
                detail += "; SELECT 1 ran"
    except ToolError as error:
        _line(FAIL, f"{name} ({row['platform']})", str(error))
        return False
    _line(OK, f"{name} ({row['platform']}, {row['account'] or 'no account'})",
          f"{detail} in {time.perf_counter() - started:.1f} s")
    return True


def check_connections(settings: Settings, email: str, with_query: bool = False) -> int:
    from sqlalchemy import func, select

    from backend.core.connection_manager import connections
    from backend.core.connection_manager.credentials import linked_usernames
    from backend.main import create_app
    from backend.shared.models import User

    app = create_app(settings)
    state = app.state
    with state.db.session() as session:
        users = session.scalars(select(User).where(
            func.lower(User.email) == email.strip().lower())).all()
        if len(users) != 1:
            print(f"Found {len(users)} people with email {email}: sign in once at "
                  f"{settings.public_base_url} and add connections first.")
            return 1
        user = users[0]
        accounts = linked_usernames(session, state.secrets, user.id)
        rows = []
        for c in connections.list_for_user(session, user.id):
            models = (c.settings or {}).get("semantic_models") or []
            rows.append({"name": c.name, "platform": c.platform,
                         "schemas": list(c.scope.get("schemas", [])),
                         "model": models[0].get("id") if connections.is_model(c) else None,
                         "account": accounts.get(c.credential_id, "")})
        user_id = user.id
    if not rows:
        print(f"{email} has no connections yet. Add them at {settings.public_base_url}/app/setup")
        return 1
    print(f"Checking {len(rows)} connection(s) of {email} against the real platforms")

    async def run_all() -> list[bool]:
        async with app.router.lifespan_context(app):
            return [await _check_one(state, user_id, row, with_query) for row in rows]

    results = asyncio.run(run_all())
    print(f"{sum(results)} of {len(results)} passed.")
    return 0 if all(results) else 1
