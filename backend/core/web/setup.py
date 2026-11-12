"""
The guided setup's JSON API, behind the React wizard (frontend/src/pages/Setup.tsx).

  GET  /api/session                         who is signed in, the CSRF token, Claude's request
  GET  /api/setup/platforms                 step 1: what this server can set up
  GET  /api/setup/{p}/sign-in               step 2: how to sign in (POST: the person's choice)
  GET  /api/setup/{p}/workspaces            step 3: workspaces (sign in first, if needed)
  GET  /api/setup/{p}/ws/{w}/warehouses     step 4 (POST: connect the ticked ones)
  GET  /api/setup/{p}/ws/{w}/warehouses/{h}/schemas   narrow a warehouse (POST: save)
  GET  /api/connections/{id}/schemas        a saved warehouse's schemas, any platform (PUT: save)
  GET  /api/setup/question                  step 5a: "also connect a semantic model?"
  GET  /api/setup/{p}/ws/{w}/models         step 5b (POST: connect the ticked ones)
  GET  /api/setup/status                    step 6: one status per resource
  POST /api/setup/items/{id}/retry|remove   per-resource actions
  GET  /api/setup/instructions              step 7 (POST /api/setup/finish: save, finish)
  GET  /api/connect                         Claude's Connect page (POST approve / deny)

Inside Claude's sign-in the OAuth request lives in its OAuthFlow row; only its id
(`flow`) travels, regex-checked. The authorization code is issued only by Finish
or Allow -- explicit, CSRF-protected posts -- through `complete_flow`, so the
redirect address and `state` are the ones Claude sent and were validated at
/oauth/authorize. Nothing here builds a Claude URL; the app just goes where
`redirect` says.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from backend.core.auth.oauth.authorize import FlowNotFound, complete_flow, deny_flow
from backend.core.auth.users import MAX_INSTRUCTIONS, set_instructions
from backend.core.connection_manager import connections, resources
from backend.core.connection_manager.credentials import NotLinked, linked_usernames
from backend.core.connection_manager.drivers import (
    MODEL,
    WAREHOUSE,
    AccountPlatformSetup,
    PlatformSetup,
    Resource,
    SetupProblem,
    platform_for,
    platform_statuses,
)
from backend.core.connection_manager.resources import Choice
from backend.core.dynamic_registry.base import AdapterError, CredentialRejected
from backend.core.security.audit import record
from backend.core.web.common import (
    ApiError,
    bind_flow,
    change,
    flow_id,
    flow_info,
    parse_uuid,
    person,
    text,
    texts,
    with_query,
)
from backend.shared.models import DataConnection, OAuthFlow, SetupItem, User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["setup"])

MAX_SCHEMAS = 200
EXPIRED = ("This sign-in from Claude has expired. Go back to Claude and press Connect again "
           "-- the resources you connected are kept.")


# --- who is asking ------------------------------------------------------------------------------


@router.get("/session")
def session_info(request: Request, flow: str = "") -> dict[str, Any]:
    state = request.app.state
    flow = flow_id(flow)
    browser = state.sessions.read(request)
    info = flow_info(request, flow)
    public = {"signedIn": False, "claude": {"app": info["app"]} if info else None,
              "mcpUrl": state.settings.mcp_url,
              "connectorsUrl": state.settings.claude_connectors_url}
    if browser is None:
        return public
    bind_flow(request, flow, browser.user_id)
    with state.db.session() as session:
        user = session.get(User, browser.user_id)
        if user is None:
            return public
        return {**public, "signedIn": True, "email": user.email, "name": user.display_name,
                "csrf": browser.csrf, "isAdmin": user.is_admin}


# --- shared pieces ------------------------------------------------------------------------------


def _driver(request: Request, key: str, user_id) -> PlatformSetup:
    driver = platform_for(request.app.state, key, user_id)
    if driver is not None:
        return driver
    status = next((s for s in platform_statuses(request.app.state) if s.key == key), None)
    if status is not None and status.setup_url:
        raise ApiError(409, "needs_setup", status.why_not, setupUrl=status.setup_url)
    raise ApiError(404, "unavailable", "This platform is not available on this server.")


def _platform(driver: PlatformSetup) -> dict[str, Any]:
    return {"key": driver.key, "label": driver.label, "account": driver.account(),
            "hasModels": driver.has_models, "hasCatalogs": driver.has_catalogs,
            "warehouseNote": driver.warehouse_note, "modelNote": driver.model_note}


def _sign_in(driver: PlatformSetup, back_to: str, reason: str = "") -> ApiError:
    """The person must sign in to the platform; the app sends them to `signInUrl`."""
    return ApiError(428, "sign_in", reason, signInUrl=driver.sign_in_url(back_to),
                    platform={"key": driver.key, "label": driver.label,
                              "signInLabel": driver.sign_in_label.split(" (")[0]})


def _platform_error(driver: PlatformSetup, error: Exception, back_to: str) -> ApiError:
    if isinstance(error, NotLinked):
        return _sign_in(driver, back_to)
    if isinstance(error, CredentialRejected):
        # Also what a Microsoft sign-in made for another platform gets when it hasn't
        # been granted this one yet: signing in again asks for exactly that.
        return _sign_in(driver, back_to,
                        f"{driver.label} didn't accept your current sign-in: it has expired, "
                        f"or {driver.label} still needs your permission. Sign in again.")
    if isinstance(error, SetupProblem):
        return ApiError(404, "not_found", str(error))
    return ApiError(502, "platform", f"{driver.label} couldn't be reached: {error}")


def _guard(driver: PlatformSetup, back_to: str, work):
    """Run a platform lookup; platform failures become the errors the app understands."""
    if not driver.linked():
        raise _sign_in(driver, back_to)
    try:
        return work()
    except (NotLinked, AdapterError, SetupProblem) as error:
        raise _platform_error(driver, error, back_to) from error


def _connections(request: Request, user_id) -> dict:
    """connection id -> its name and the account it signs in as."""
    state = request.app.state
    with state.db.session() as session:
        accounts = linked_usernames(session, state.secrets, user_id)
        return {c.id: {"name": c.name, "account": accounts.get(c.credential_id, "")}
                for c in connections.list_for_user(session, user_id)}


def _item(item: SetupItem | None, made: dict | None = None,
          labels: dict[str, str] | None = None) -> dict[str, Any] | None:
    if item is None:
        return None
    connection = (made or {}).get(item.connection_id) if item.connection_id else None
    return {"id": str(item.id), "kind": "model" if item.kind == MODEL else "warehouse",
            "platform": item.platform,
            "platformLabel": (labels or {}).get(item.platform, item.platform),
            "workspaceId": item.workspace_id, "workspaceName": item.workspace_name,
            "resourceId": item.resource_id, "name": item.resource_name or item.resource_id,
            "catalog": item.catalog, "schemas": list(item.schemas or []),
            "status": item.status, "error": item.error if item.status == "failed" else "",
            "connectionName": connection["name"] if connection else None,
            "account": connection["account"] if connection else ""}


def _earlier(request: Request, user_id, key: str, workspace_id: str) -> dict[str, SetupItem]:
    return {i.resource_key: i for i in resources.items_for(request.app.state.db, user_id)
            if i.platform == key and i.workspace_id == workspace_id}


def _connect(request: Request, user_id, driver: PlatformSetup, workspace: Resource,
             choices: list[Choice], failures: dict[str, str]) -> None:
    """Save each pick and connect it; picks that already failed to list get that reason."""
    state = request.app.state
    ids = resources.record_choices(state.db, user_id, driver.key, workspace, choices)
    for choice, item_id in zip(choices, ids, strict=True):
        if choice.key in failures:
            resources.mark_failed(state.db, user_id, item_id, failures[choice.key])
        else:
            resources.connect_item(state, user_id, item_id)


def _back(key: str, flow: str, then: str = "") -> str:
    """Where a platform sign-in returns to: this platform's step in the wizard."""
    return with_query(f"/app/setup/{key}", then=then, flow=flow)


# --- 1-3: platforms, sign in, workspaces ---------------------------------------------------------


def _warehouse_platform(request: Request, user_id) -> str | None:
    with request.app.state.db.session() as session:
        return connections.warehouse_platform(session, user_id)


@router.get("/setup/platforms")
def platforms(request: Request) -> dict[str, Any]:
    browser = request.app.state.sessions.read(request)
    return {"platforms": [
        {"key": s.key, "label": s.label, "available": s.available, "how": s.how,
         "whyNot": s.why_not, "setupUrl": s.setup_url}
        for s in platform_statuses(request.app.state)],
        # Set once the person has a warehouse: more are added on that same platform.
        "warehousePlatform": _warehouse_platform(request, browser.user_id) if browser else None}


@router.get("/setup/{key}/workspaces")
async def workspaces(request: Request, key: str, flow: str = "",
                     then: str = "") -> dict[str, Any]:
    browser = person(request)
    flow, then = flow_id(flow), ("models" if then == "models" else "")
    driver = _driver(request, key, browser.user_id)
    bind_flow(request, flow, browser.user_id)
    found = await run_in_threadpool(_guard, driver, _back(key, flow, then), driver.workspaces)
    driver.flow = flow
    # Databricks workspaces and Snowflake accounts are the person's own: they can add more.
    adds = isinstance(driver, AccountPlatformSetup)
    return {"platform": _platform(driver),
            "workspaces": [{"id": w.id, "name": w.name, "detail": w.detail} for w in found],
            "addUrl": driver.sign_in_page("") if adds else ""}


@router.get("/setup/{key}/sign-in")
def sign_in_options(request: Request, key: str, flow: str = "") -> dict[str, Any]:
    browser = person(request)
    driver = _driver(request, key, browser.user_id)
    driver.flow = flow_id(flow)
    bind_flow(request, driver.flow, browser.user_id)
    return {
        "platform": {"key": driver.key, "label": driver.label},
        "methods": [{"key": m.key, "label": m.label, "description": m.description,
                     "recommended": m.recommended, "available": m.available,
                     "whyNot": m.why_not, "redirectUri": m.redirect_uri,
                     "fields": [{"name": f.name, "label": f.label, "placeholder": f.placeholder,
                                 "value": f.value, "secret": f.secret, "required": f.required,
                                 "hint": f.hint} for f in m.fields]}
                    for m in driver.methods()],
        "accounts": driver.accounts(),
    }


@router.post("/setup/{key}/sign-in")
async def save_sign_in(request: Request, key: str) -> dict[str, Any]:
    """The person's sign-in choice: checked (with the platform when it can be), kept."""
    browser, data = await change(request)
    driver = _driver(request, key, browser.user_id)
    flow = flow_id(text(data, "flow"))
    values = data.get("values") if isinstance(data.get("values"), dict) else {}

    def save() -> str:
        # Pages the driver sends the person to keep Claude's sign-in id.
        driver.flow = flow
        try:
            # Back to the step that asked for the sign-in -- only ever one of this
            # platform's own setup pages.
            wanted = text(data, "next", limit=500)
            safe = wanted.startswith(f"/app/setup/{key}") and "//" not in wanted
            next_page = wanted if safe else _back(key, flow)
            return driver.save_sign_in(text(data, "method"), values, next_page)
        except SetupProblem as error:
            raise ApiError(400, "sign_in_refused", str(error)) from error

    redirect = await run_in_threadpool(save)
    record(request.app.state.db, org_id=_org(request, browser.user_id),
           user_id=browser.user_id, tool="platform_sign_in", outcome="ok", duration_ms=0,
           platform=key, statement=text(data, "method"))
    return {"redirect": redirect}


def _org(request: Request, user_id) -> uuid.UUID:
    with request.app.state.db.session() as session:
        user = session.get(User, user_id)
        return user.org_id if user else uuid.UUID(int=0)


# --- 4: warehouses -------------------------------------------------------------------------------


def _warehouses(request: Request, user_id, driver: PlatformSetup, workspace_id: str, flow: str,
                catalog: str) -> dict[str, Any]:
    def work():
        workspace, seen = driver.locate(workspace_id)
        return (workspace, seen, driver.warehouses(workspace.id),
                driver.catalogs(workspace.id) if driver.has_catalogs else [])

    workspace, seen, warehouses, catalogs = _guard(driver, _back(driver.key, flow), work)
    earlier = _earlier(request, user_id, driver.key, workspace.id)
    made = _connections(request, user_id)
    if driver.has_catalogs:
        catalog = catalog if catalog in catalogs else next(
            (i.catalog for i in earlier.values() if i.catalog in catalogs),
            catalogs[0] if catalogs else "")
    else:
        catalog = ""
    return {"platform": _platform(driver), "workspace": _workspace(workspace, seen, driver),
            "catalogs": catalogs, "catalog": catalog,
            "warehouses": [{"id": w.id, "name": w.name, "detail": w.detail,
                            "item": _item(earlier.get(Choice(WAREHOUSE, w, catalog).key), made)}
                           for w in warehouses]}


def _workspace(workspace: Resource, seen: int, driver: PlatformSetup) -> dict[str, Any]:
    # `only`: the person sees just this one, so the app skipped the workspace picker
    # and Back must skip it too. (Never for their own workspaces: they may add another.)
    only = seen == 1 and not isinstance(driver, AccountPlatformSetup)
    return {"id": workspace.id, "name": workspace.name, "only": only}


@router.get("/setup/{key}/ws/{workspace_id}/warehouses")
async def list_warehouses(request: Request, key: str, workspace_id: str, flow: str = "",
                          catalog: str = "") -> dict[str, Any]:
    browser = person(request)
    driver = _driver(request, key, browser.user_id)
    return await run_in_threadpool(_warehouses, request, browser.user_id, driver, workspace_id,
                                   flow_id(flow), catalog[:200])


@router.post("/setup/{key}/ws/{workspace_id}/warehouses")
async def connect_warehouses(request: Request, key: str, workspace_id: str) -> dict[str, Any]:
    browser, data = await change(request)
    driver = _driver(request, key, browser.user_id)
    picked, catalog, flow = texts(data, "ids"), text(data, "catalog"), flow_id(text(data, "flow"))
    if not picked:
        raise ApiError(400, "nothing", "Tick at least one, then press Connect.")
    return await run_in_threadpool(_connect_warehouses, request, browser.user_id, driver,
                                   workspace_id, flow, picked, catalog)


def _connect_warehouses(request: Request, user_id, driver: PlatformSetup, workspace_id: str,
                        flow: str, picked: list[str], catalog: str) -> dict[str, Any]:
    def work():
        # Only ids come from the browser: look everything up again as the person.
        workspace = driver.workspace(workspace_id)
        visible = {w.id: w for w in driver.warehouses(workspace.id)}
        if any(i not in visible for i in picked):
            raise SetupProblem("Pick warehouses from the list -- some are not visible to you.")
        if driver.has_catalogs and catalog not in driver.catalogs(workspace.id):
            raise SetupProblem("Pick a catalog from the list.")
        return workspace, visible

    workspace, visible = _guard(driver, _back(driver.key, flow), work)
    earlier = _earlier(request, user_id, driver.key, workspace.id)
    scope_catalog = catalog if driver.has_catalogs else ""
    choices, failures = [], {}
    for warehouse_id in picked:
        warehouse = visible[warehouse_id]
        key = Choice(WAREHOUSE, warehouse, scope_catalog).key
        try:
            real = driver.schemas(workspace.id, warehouse, catalog)
        except CredentialRejected as error:
            raise _platform_error(driver, error, _back(driver.key, flow)) from error
        except (NotLinked, AdapterError) as error:
            real, failures[key] = [], f"Couldn't list its schemas: {error}"
        prior = earlier.get(key)
        # Connecting again keeps a schema limit chosen before; otherwise every schema now.
        kept = [s for s in (prior.schemas if prior else []) if s in real]
        chosen = kept or real
        if not chosen and key not in failures:
            failures[key] = "There are no schemas you can read in this warehouse."
        choices.append(Choice(WAREHOUSE, warehouse, scope_catalog, tuple(chosen)))
    _connect(request, user_id, driver, workspace, choices, failures)
    return _warehouses(request, user_id, driver, workspace.id, flow, catalog)


def _one_warehouse(driver: PlatformSetup, workspace_id: str, warehouse_id: str):
    workspace = driver.workspace(workspace_id)
    warehouse = next((w for w in driver.warehouses(workspace.id) if w.id == warehouse_id), None)
    if warehouse is None:
        raise SetupProblem("That warehouse isn't visible to you.")
    return workspace, warehouse


@router.get("/setup/{key}/ws/{workspace_id}/warehouses/{warehouse_id}/schemas")
async def list_schemas(request: Request, key: str, workspace_id: str, warehouse_id: str,
                       flow: str = "", catalog: str = "") -> dict[str, Any]:
    browser = person(request)
    driver = _driver(request, key, browser.user_id)
    flow, catalog = flow_id(flow), catalog[:200]

    def work():
        workspace, warehouse = _one_warehouse(driver, workspace_id, warehouse_id)
        return workspace, warehouse, driver.schemas(workspace.id, warehouse, catalog)

    workspace, warehouse, schemas = await run_in_threadpool(
        _guard, driver, _back(key, flow), work)
    prior = _earlier(request, browser.user_id, key, workspace.id).get(
        Choice(WAREHOUSE, warehouse, catalog if driver.has_catalogs else "").key)
    ticked = list(prior.schemas) if prior and prior.schemas else schemas
    return {"warehouse": {"id": warehouse.id, "name": warehouse.name},
            "schemas": schemas, "ticked": [s for s in ticked if s in schemas]}


@router.post("/setup/{key}/ws/{workspace_id}/warehouses/{warehouse_id}/schemas")
async def save_schemas(request: Request, key: str, workspace_id: str,
                       warehouse_id: str) -> dict[str, Any]:
    browser, data = await change(request)
    driver = _driver(request, key, browser.user_id)
    chosen = texts(data, "schemas", limit=MAX_SCHEMAS)
    catalog, flow = text(data, "catalog"), flow_id(text(data, "flow"))
    if not chosen:
        raise ApiError(400, "schemas", "Pick at least one schema.")

    def save():
        workspace, warehouse = _guard(driver, _back(key, flow),
                                      lambda: _one_warehouse(driver, workspace_id, warehouse_id))
        # The schemas are checked against the real ones when the item connects.
        choice = Choice(WAREHOUSE, warehouse, catalog if driver.has_catalogs else "",
                        tuple(chosen))
        _connect(request, browser.user_id, driver, workspace, [choice], {})
        return _warehouses(request, browser.user_id, driver, workspace.id, flow, catalog)

    return await run_in_threadpool(save)


# --- a saved connection's schemas (My connections), on every platform ----------------------------


def _schema_target(request: Request, user_id, connection_id: str):
    """(id, platform, scope, settings) of the person's own warehouse connection, and its driver."""
    cid = parse_uuid(connection_id)
    with request.app.state.db.session() as session:
        row = session.get(DataConnection, cid) if cid else None
        if row is None or row.user_id != user_id:
            raise ApiError(404, "not_found", "That connection no longer exists.")
        if connections.is_model(row):
            raise ApiError(400, "no_schemas", "A semantic model has no schemas to limit.")
        found = row.id, row.platform, dict(row.scope or {}), dict(row.settings or {})
    return found, _driver(request, found[1], user_id)


def _readable(driver: PlatformSetup, scope: dict, settings: dict) -> list[str]:
    # A platform sign-in asked for here comes back to My connections.
    return _guard(driver, "/app", lambda: driver.connection_schemas(scope, settings))


@router.get("/connections/{connection_id}/schemas")
async def connection_schemas(request: Request, connection_id: str) -> dict[str, Any]:
    browser = person(request)

    def work():
        (_, _, scope, settings), driver = _schema_target(request, browser.user_id, connection_id)
        real = _readable(driver, scope, settings)
        return {"schemas": real, "ticked": [s for s in scope.get("schemas", []) if s in real]}

    return await run_in_threadpool(work)


@router.put("/connections/{connection_id}/schemas")
async def save_connection_schemas(request: Request, connection_id: str) -> dict[str, Any]:
    browser, data = await change(request)
    picked = set(texts(data, "schemas", limit=MAX_SCHEMAS))
    if not picked:
        raise ApiError(400, "schemas", "Pick at least one schema.")
    return await run_in_threadpool(_save_connection_schemas, request, browser.user_id,
                                   connection_id, picked)


def _save_connection_schemas(request: Request, user_id, connection_id: str,
                             picked: set[str]) -> dict[str, Any]:
    state = request.app.state
    (cid, platform, scope, settings), driver = _schema_target(request, user_id, connection_id)
    # Only schemas the person can read now, listed again as them: never the browser's word.
    real = _readable(driver, scope, settings)
    missing = sorted(picked - set(real))
    if missing:
        raise ApiError(400, "schemas", "You can't read these schemas: " + ", ".join(missing))
    chosen = [s for s in real if s in picked]
    with state.db.session() as session:
        row = session.get(DataConnection, cid)
        if row is None or row.user_id != user_id:
            raise ApiError(404, "not_found", "That connection no longer exists.")
        updated = {**(row.scope or {}), "schemas": chosen}
        if updated.get("default_schema") not in chosen:
            updated.pop("default_schema", None)
        row.scope = updated
        # The guided setup keeps the same limit, so connecting this warehouse again keeps it.
        for item in session.scalars(select(SetupItem).where(SetupItem.connection_id == cid)):
            item.schemas = list(chosen)
        org_id = row.org_id
    record(state.db, org_id=org_id, user_id=user_id, tool="edit_schemas", outcome="ok",
           duration_ms=0, connection_id=cid, platform=platform, statement=", ".join(chosen))
    return {"ok": True, "schemas": chosen}


# --- 5: the semantic-model question, and the models ----------------------------------------------


@router.get("/setup/question")
def models_question(request: Request, flow: str = "") -> dict[str, Any]:
    """Optional: connect semantic models too? Only Fabric has them, whatever came before."""
    browser = person(request)
    state = request.app.state
    bind_flow(request, flow_id(flow), browser.user_id)
    fabric = next(s for s in platform_statuses(state) if s.key == "fabric")
    account = ""
    if not fabric.setup_url:
        driver = platform_for(state, "fabric", browser.user_id)
        account = driver.account() if driver is not None and driver.linked() else ""
    connected = [i.resource_name or i.resource_id
                 for i in resources.items_for(state.db, browser.user_id)
                 if i.kind == MODEL and i.status == "connected"]
    # The app goes to the Fabric workspaces with `then=models`: an existing Microsoft
    # sign-in is reused there, a missing one is asked for there.
    return {"setupUrl": fabric.setup_url, "account": account, "connected": connected,
            "warehousePlatform": _warehouse_platform(request, browser.user_id)}


def _models(request: Request, user_id, driver: PlatformSetup, workspace_id: str,
            flow: str) -> dict[str, Any]:
    if not driver.has_models:
        raise ApiError(404, "no_models", f"{driver.label} has no semantic models.")

    def work():
        workspace, seen = driver.locate(workspace_id)
        return workspace, seen, driver.models(workspace.id)

    workspace, seen, models = _guard(driver, _back(driver.key, flow, "models"), work)
    earlier = _earlier(request, user_id, driver.key, workspace.id)
    made = _connections(request, user_id)
    return {"platform": _platform(driver), "workspace": _workspace(workspace, seen, driver),
            "models": [{"id": m.id, "name": m.name, "detail": m.detail,
                        "item": _item(earlier.get(Choice(MODEL, m).key), made)}
                       for m in models]}


@router.get("/setup/{key}/ws/{workspace_id}/models")
async def list_models(request: Request, key: str, workspace_id: str,
                      flow: str = "") -> dict[str, Any]:
    browser = person(request)
    driver = _driver(request, key, browser.user_id)
    return await run_in_threadpool(_models, request, browser.user_id, driver, workspace_id,
                                   flow_id(flow))


@router.post("/setup/{key}/ws/{workspace_id}/models")
async def connect_models(request: Request, key: str, workspace_id: str) -> dict[str, Any]:
    browser, data = await change(request)
    driver = _driver(request, key, browser.user_id)
    picked, flow = texts(data, "ids"), flow_id(text(data, "flow"))
    if not picked:
        raise ApiError(400, "nothing", "Tick at least one, then press Connect.")
    if not driver.has_models:
        raise ApiError(404, "no_models", f"{driver.label} has no semantic models.")

    def save():
        def work():
            workspace = driver.workspace(workspace_id)
            visible = {m.id: m for m in driver.models(workspace.id)}
            if any(i not in visible for i in picked):
                raise SetupProblem("Pick semantic models from the list -- some are not "
                                   "visible to you.")
            return workspace, visible

        workspace, visible = _guard(driver, _back(key, flow, "models"), work)
        _connect(request, browser.user_id, driver, workspace,
                 [Choice(MODEL, visible[i]) for i in picked], {})
        return _models(request, browser.user_id, driver, workspace.id, flow)

    return await run_in_threadpool(save)


# --- 6: connection status ------------------------------------------------------------------------


def _status(request: Request, user_id, flow: str) -> dict[str, Any]:
    state = request.app.state
    items = resources.items_for(state.db, user_id)
    made = _connections(request, user_id)
    labels = {s.key: s.label for s in platform_statuses(state)}
    with state.db.session() as session:
        active = sum(1 for c in connections.list_for_user(session, user_id)
                     if c.status == "active")
    counts = {s: sum(1 for i in items if i.status == s)
              for s in ("pending", "connecting", "connected", "failed")}
    expired = bool(flow) and flow_info(request, flow) is None
    ready = (active > 0 and not expired
             and not (counts["pending"] or counts["connecting"] or counts["failed"]))
    return {"items": [_item(i, made, labels) for i in items], "counts": counts,
            "active": active, "expired": expired, "ready": ready}


@router.get("/setup/status")
def status(request: Request, flow: str = "") -> dict[str, Any]:
    browser = person(request)
    flow = flow_id(flow)
    bind_flow(request, flow, browser.user_id)
    return _status(request, browser.user_id, flow)


@router.post("/setup/items/{item_id}/{action}")
async def item_action(request: Request, item_id: str, action: str) -> dict[str, Any]:
    browser, data = await change(request)
    if action not in ("retry", "remove"):
        raise ApiError(404, "not_found", "Unknown action.")
    state = request.app.state
    parsed = parse_uuid(item_id)
    # Owner check first: another person's item id behaves exactly like a missing one.
    if parsed is None or not resources.owns_item(state.db, browser.user_id, parsed):
        raise ApiError(404, "not_found", "That item no longer exists.")
    if action == "remove":
        if not resources.remove_item(state.db, browser.user_id, parsed):
            raise ApiError(409, "remove_connected",
                           "A connected resource is managed on the dashboard.")
    else:
        await run_in_threadpool(resources.connect_item, state, browser.user_id, parsed)
    return _status(request, browser.user_id, flow_id(text(data, "flow")))


# --- 7: instructions, and finishing ---------------------------------------------------------------


def _active_names(request: Request, user_id) -> list[str]:
    with request.app.state.db.session() as session:
        return [c.name for c in connections.list_for_user(session, user_id)
                if c.status == "active"]


@router.get("/setup/instructions")
def instructions(request: Request, flow: str = "") -> dict[str, Any]:
    browser = person(request)
    flow = flow_id(flow)
    bind_flow(request, flow, browser.user_id)
    info = flow_info(request, flow)
    with request.app.state.db.session() as session:
        user = session.get(User, browser.user_id)
        saved, email = (user.instructions, user.email) if user else ("", "")
    return {"text": saved, "max": MAX_INSTRUCTIONS,
            "connections": _active_names(request, browser.user_id),
            "claude": {"app": info["app"], "email": email} if info else None,
            "expired": bool(flow) and info is None}


@router.post("/setup/finish")
async def finish(request: Request) -> dict[str, Any]:
    browser, data = await change(request)
    return await run_in_threadpool(_finish, request, browser, data)


def _finish(request: Request, browser, data: dict[str, Any]) -> dict[str, Any]:
    state = request.app.state
    flow = flow_id(text(data, "flow"))
    if isinstance(data.get("instructions"), str):
        # Saved first, so the text is kept even if the sign-in turns out to have expired.
        with state.db.session() as session:
            if not set_instructions(session, browser.user_id, data["instructions"]):
                raise ApiError(400, "too_long",
                               f"That is too long: keep it to {MAX_INSTRUCTIONS:,} characters.")
    items = resources.items_for(state.db, browser.user_id)
    if any(i.status == "connecting" for i in items):
        raise ApiError(409, "busy", "Some resources are still connecting. Wait a moment.")
    if any(i.status == "pending" for i in items):
        raise ApiError(409, "pending", "Some resources are not connected yet. Connect or "
                                       "remove them first.")
    if any(i.status == "failed" for i in items):
        # Never "setup complete" over an unresolved resource: retry it or remove it.
        raise ApiError(409, "failed", "Some resources failed to connect. Retry or remove "
                                      "them first -- the ones that connected stay connected.")
    with state.db.session() as session:
        has_active = session.scalars(select(DataConnection.id).where(
            DataConnection.user_id == browser.user_id,
            DataConnection.status == "active")).first() is not None
    if not has_active:
        raise ApiError(409, "none", "Connect at least one resource first, so Claude has "
                                    "something to query.")
    if not flow:
        return {"redirect": "/app/setup/done"}
    return {"redirect": _complete(request, browser, flow, allow=True)}


def _complete(request: Request, browser, flow: str, *, allow: bool) -> str:
    state = request.app.state
    try:
        with state.db.session() as session:
            row = session.get(OAuthFlow, flow)
            if row is not None and row.user_id not in (None, browser.user_id):
                log.warning("Claude sign-in refused: flow bound to another user")
                raise ApiError(403, "other_user", "This sign-in from Claude was started by "
                                                  "another account. Go back to Claude and "
                                                  "press Connect again.")
            # Claude's registered return address and its own `state`, both checked at
            # /oauth/authorize.
            if allow:
                return complete_flow(session, flow, browser.user_id, state.settings.issuer)
            return deny_flow(session, flow, state.settings.issuer)
    except FlowNotFound as error:
        raise ApiError(410, "expired", EXPIRED) from error


@router.get("/setup/done")
def done(request: Request) -> dict[str, Any]:
    browser = person(request)
    with request.app.state.db.session() as session:
        accounts = linked_usernames(session, request.app.state.secrets, browser.user_id)
        rows = connections.list_for_user(session, browser.user_id)
        found = [{"name": c.name, "platform": c.platform, "active": c.status == "active",
                  "kind": "model" if connections.is_model(c) else "warehouse",
                  "account": accounts.get(c.credential_id, "")} for c in rows]
    return {"connections": found, "chatUrl": request.app.state.settings.claude_chat_url}


# --- Claude's Connect page -----------------------------------------------------------------------


@router.get("/connect")
def connect_info(request: Request, flow: str = "") -> dict[str, Any]:
    browser = person(request)
    flow = flow_id(flow)
    info = flow_info(request, flow)
    if info is None:
        raise ApiError(410, "expired", "This connection request has expired. Go back to "
                                       "Claude and press Connect again.")
    bind_flow(request, flow, browser.user_id)
    with request.app.state.db.session() as session:
        user = session.get(User, browser.user_id)
        rows = connections.list_for_user(session, browser.user_id)
        return {"app": info["app"], "email": user.email if user else "",
                "connections": [{"name": c.name, "platform": c.platform} for c in rows]}


@router.post("/connect/{decision}")
async def connect_decide(request: Request, decision: str) -> dict[str, Any]:
    browser, data = await change(request)
    if decision not in ("approve", "deny"):
        raise ApiError(404, "not_found", "Unknown decision.")
    flow = flow_id(text(data, "flow"))
    if not flow:
        raise ApiError(410, "expired", EXPIRED)
    return {"redirect": await run_in_threadpool(_complete, request, browser, flow,
                                                allow=decision == "approve")}
