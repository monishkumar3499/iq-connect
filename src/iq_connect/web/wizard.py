"""
The "New connection" wizard: platform -> how to sign in -> which data -> name.

Reachable from the dashboard and from the Connect page while Claude is
signing someone in; in that case `flow` (the sign-in in progress) is carried
through every step, and the person lands back on the Connect page to press
Allow.

Choices are made only from what the person can actually see, listed with
their own platform login. On the final post nothing from the browser is
trusted beyond ids: the warehouse is looked up again by id (its address and
name come from Fabric, not the form), and the chosen schemas must be among
the warehouse's real schemas.

Sign-in methods offered never store a platform password: Fabric uses the
person's Microsoft account; the demo needs none.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from ..adapters.base import AdapterError, CredentialRejected
from ..adapters.databricks.adapter import AZURE_DATABRICKS_SCOPE
from ..adapters.fabric.scopes import FABRIC_API_SCOPE, SQL_SCOPE
from ..connections import service as connections
from ..credentials.linked import (
    NotLinked,
    linked_account,
    linked_microsoft_account,
    microsoft_token,
    provider_token,
)
from ..db.models import DataConnection, User
from ..setup.fabric import warehouse_probe
from .pages import app_page, escape, message

router = APIRouter(tags=["wizard"])

MAX_NAME = 100
MAX_NOTES = 2000
_FLOW = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
_BASE = "/app/connections/new"

_STEPS = ("Platform", "Sign in", "Data", "Name")


# --- small helpers -------------------------------------------------------------------


def _flow(value: str | None) -> str:
    """A Claude sign-in id to carry along, or "" -- never anything else."""
    return value if value and _FLOW.match(value) else ""


def _url(path: str, flow: str, **params: str) -> str:
    query = {k: v for k, v in {**params, "flow": flow}.items() if v}
    return path + ("?" + urlencode(query) if query else "")


def _finished(flow: str) -> RedirectResponse:
    target = f"/connect?{urlencode({'flow': flow})}" if flow else "/?done=added"
    return RedirectResponse(target, status_code=303)


def _steps(current: int) -> str:
    items = []
    for number, label in enumerate(_STEPS, start=1):
        state = "ok" if number < current else ("plain" if number > current else "warn")
        items.append(f"<span class='pill {state}'>{number}. {label}</span>")
    return "<div class='conn-h' aria-label='Steps'>" + " ".join(items) + "</div>"


def _shell(request: Request, browser, title: str, step: int, inner: str, flow: str,
           status_code: int = 200) -> Response:
    with request.app.state.db.session() as session:
        user = session.get(User, browser.user_id)
        email = user.email if user else ""
    back = (f"<a href='{escape(_url('/connect', flow))}'>Back to connecting Claude</a>"
            if flow else "<a href='/'>Back to dashboard</a>")
    body = (
        "<section class='panel'><div class='panel-h'>"
        f"<h2>{escape(title)}</h2>{_steps(step)}<span class='muted'>{back}</span></div>"
        f"<div class='panel-b'>{inner}</div></section>"
    )
    return app_page(title, email=email, csrf=browser.csrf, body_html=body,
                    status_code=status_code)


def _signed_in(request: Request, flow: str):
    browser = request.app.state.sessions.read(request)
    if browser is None:
        return None, RedirectResponse(
            "/auth/login?" + urlencode({"next": _url(_BASE, flow)}), status_code=302)
    return browser, None


def _hidden(**fields: str) -> str:
    return "".join(f"<input type='hidden' name='{escape(k)}' value='{escape(v)}'>"
                   for k, v in fields.items() if v)


def _name_fields(default_name: str) -> str:
    return (
        "<label for='name'>Connection name</label>"
        f"<input id='name' name='name' type='text' maxlength='{MAX_NAME}' required "
        f"value='{escape(default_name)}'>"
        "<p class='muted'>You and Claude use this name to pick the connection.</p>"
        "<label for='notes'>Notes for Claude (optional)</label>"
        f"<textarea id='notes' name='notes' maxlength='{MAX_NOTES}'></textarea>"
        "<p class='muted'>Units, definitions, what to watch out for. Claude sees these with "
        "every answer from this connection.</p>"
    )


def _check_name(session, user_id, name: str, notes: str) -> str | None:
    name = name.strip()
    if not name:
        return "Give the connection a name."
    if len(name) > MAX_NAME or len(notes) > MAX_NOTES:
        return "The name or notes are too long."
    clash = session.scalars(select(DataConnection.id).where(
        DataConnection.user_id == user_id, func.lower(DataConnection.name) == name.lower(),
    )).first()
    return "You already have a connection with that name." if clash else None


# --- step 1: platform ------------------------------------------------------------------


@router.get(_BASE)
def choose_platform(request: Request, flow: str = "") -> Response:
    flow = _flow(flow)
    browser, redirect = _signed_in(request, flow)
    if redirect:
        return redirect
    state = request.app.state

    def card(title: str, how: str, href: str | None, why_not: str = "") -> str:
        action = (f"<a href='{escape(href)}'>Choose {escape(title)}</a>" if href
                  else f"<span class='pill plain'>{escape(why_not)}</span>")
        return (f"<div class='conn'><div class='conn-h'><strong>{escape(title)}</strong></div>"
                f"<p class='muted'>Sign in with: {escape(how)}</p>{action}</div>")

    cards = []
    if state.entra is not None:
        cards.append(card("Microsoft Fabric", "your Microsoft account (SSO)",
                          _url(f"{_BASE}/fabric", flow)))
    else:
        cards.append(card("Microsoft Fabric", "your Microsoft account (SSO)", None,
                          "needs the server's Microsoft (Entra) settings"))
    how = _databricks_sign_in(state)
    if how is not None:
        cards.append(card("Databricks", how[3], _url(f"{_BASE}/databricks", flow)))
    else:
        cards.append(card("Databricks", "Databricks or Microsoft sign-in", None,
                          "needs the server's Databricks settings"))
    cards.append(card("Snowflake", "SSO or key-pair", None, "not available yet"))
    if state.settings.demo_enabled:
        cards.append(card("Demo (sample sales data)", "nothing -- built in",
                          _url(f"{_BASE}/demo", flow)))
    inner = ("<p>Which platform holds the data? Claude only ever reads, and only the data "
             "you choose in the next steps.</p><div class='conns'>" + "".join(cards) + "</div>")
    return _shell(request, browser, "New connection", 1, inner, flow)


# --- demo ------------------------------------------------------------------------------


@router.get(f"{_BASE}/demo")
def demo_form(request: Request, flow: str = "", problem: str = "") -> Response:
    flow = _flow(flow)
    browser, redirect = _signed_in(request, flow)
    if redirect:
        return redirect
    if not request.app.state.settings.demo_enabled:
        return message("Not available", "The demo platform is switched off.", 404)
    notice = f"<div class='notice bad' role='alert'>{escape(problem)}</div>" if problem else ""
    inner = (
        f"{notice}<p>A small built-in sales database (customers and orders) -- no sign-in "
        "needed. Claude will see the <code>sales</code> schema.</p>"
        f"<form method='post' action='{_BASE}/demo'>{_hidden(csrf=browser.csrf, flow=flow)}"
        f"{_name_fields('Demo sales')}"
        "<div class='row'><button class='primary' type='submit'>Create connection</button>"
        "</div></form>"
    )
    return _shell(request, browser, "New demo connection", 4, inner, flow)


@router.post(f"{_BASE}/demo")
def demo_create(request: Request, name: str = Form(""), notes: str = Form(""),
                csrf: str = Form(""), flow: str = Form("")) -> Response:
    flow = _flow(flow)
    browser = request.app.state.sessions.read(request)
    if browser is None or not browser.csrf_matches(csrf):
        return message("Request refused", "Reload the page and try again.", 403)
    if not request.app.state.settings.demo_enabled:
        return message("Not available", "The demo platform is switched off.", 404)
    with request.app.state.db.session() as session:
        problem = _check_name(session, browser.user_id, name, notes)
        if problem is None:
            connections.create(
                session, session.get(User, browser.user_id), platform="demo",
                name=name.strip(), custom_prompt=notes.strip(),
                scope={"catalog": "demo", "schemas": ["sales"]},
            )
    if problem:
        return demo_form(request, flow=flow, problem=problem)
    return _finished(flow)


# --- Microsoft Fabric ------------------------------------------------------------------


def _fabric_api(request: Request, user_id):
    state = request.app.state
    _, token = microsoft_token(state.db, state.secrets, state.entra, user_id, FABRIC_API_SCOPE)
    return state.fabric_api(token)


def _schemas(request: Request, user_id, warehouse: dict[str, str]) -> list[str]:
    state = request.app.state
    _, token = microsoft_token(state.db, state.secrets, state.entra, user_id, SQL_SCOPE)
    adapter = state.pipeline.registry.get("fabric")
    return adapter.list_schemas(warehouse_probe(warehouse), {"access_tokens": {SQL_SCOPE: token}})


def _models(api, workspace_id: str) -> list[dict[str, str]]:
    """The workspace's semantic models; a client without the method lists none."""
    lister = getattr(api, "semantic_models", None)
    return lister(workspace_id) if lister else []


def _find(items: list[dict[str, str]], item_id: str) -> dict[str, str] | None:
    return next((item for item in items if item["id"] == item_id), None)


def _link_prompt(request, browser, flow: str, why: str = "") -> Response:
    back_here = _url(f"{_BASE}/fabric", flow)
    link = "/auth/platform/entra/start?" + urlencode({"next": back_here})
    inner = (
        (f"<div class='notice bad' role='alert'>{escape(why)}</div>" if why else "")
        + "<p>IQ Connect reads Fabric <strong>as you</strong>, with your Microsoft account, so "
        "Claude sees exactly what you are allowed to see -- and only the parts you pick next. "
        "No password is stored; Microsoft can revoke the link at any time.</p>"
        f"<div class='row'><a href='{escape(link)}'>Sign in with Microsoft</a></div>"
    )
    return _shell(request, browser, "New Fabric connection", 2, inner, flow)


@router.get(f"{_BASE}/fabric")
def fabric_steps(request: Request, flow: str = "", workspace: str = "",
                 warehouse: str = "", problem: str = "") -> Response:
    flow = _flow(flow)
    browser, redirect = _signed_in(request, flow)
    if redirect:
        return redirect
    state = request.app.state
    if state.entra is None:
        return message("Not available", "This server is not set up for Microsoft Fabric.", 404)
    if linked_microsoft_account(state.db, browser.user_id) is None:
        return _link_prompt(request, browser, flow)

    try:
        api = _fabric_api(request, browser.user_id)
        workspaces = api.workspaces()
        if not workspace:
            options = "".join(
                f"<li><a href='{escape(_url(f'{_BASE}/fabric', flow, workspace=w['id']))}'>"
                f"{escape(w['name'])}</a></li>" for w in workspaces
            ) or "<li class='empty'>Your account can't see any Fabric workspaces.</li>"
            return _shell(request, browser, "New Fabric connection", 3,
                          f"<p>Choose a workspace:</p><ul>{options}</ul>", flow)

        chosen_ws = _find(workspaces, workspace)
        if chosen_ws is None:
            return message("Not found", "That workspace isn't visible to you.", 404)
        warehouses = [w for w in api.warehouses(workspace) if w.get("sql_endpoint")]
        if not warehouse:
            def pick(w: dict[str, str]) -> str:
                return _url(f"{_BASE}/fabric", flow, workspace=workspace, warehouse=w["id"])

            options = "".join(
                f"<li><a href='{escape(pick(w))}'>{escape(w['name'])}</a></li>"
                for w in warehouses
            ) or "<li class='empty'>No warehouses with a SQL endpoint in this workspace.</li>"
            return _shell(request, browser, "New Fabric connection", 3,
                          f"<p>Workspace <strong>{escape(chosen_ws['name'])}</strong>. "
                          f"Choose a warehouse:</p><ul>{options}</ul>", flow)

        chosen_wh = _find(warehouses, warehouse)
        if chosen_wh is None:
            return message("Not found", "That warehouse isn't visible to you.", 404)
        schemas = _schemas(request, browser.user_id, chosen_wh)
        models = _models(api, workspace)
    except NotLinked:
        return _link_prompt(request, browser, flow)
    except CredentialRejected:
        return _link_prompt(request, browser, flow,
                            "Microsoft no longer accepts your linked account. Sign in again.")
    except AdapterError as error:
        return _shell(request, browser, "New Fabric connection", 3,
                      f"<div class='notice bad' role='alert'>{escape(str(error))}</div>", flow,
                      status_code=502)

    notice = f"<div class='notice bad' role='alert'>{escape(problem)}</div>" if problem else ""
    boxes = "".join(
        f"<label style='font-weight:400'><input type='checkbox' name='schemas' "
        f"value='{escape(s)}'> {escape(s)}</label>" for s in schemas
    ) or "<p class='empty'>No schemas you can read in this warehouse.</p>"
    model_boxes = ""
    if models:
        model_boxes = (
            "<p><strong>Semantic models Claude may use (optional)</strong> -- for DAX and the "
            "published measures (needs Power BI permission on the app registration).</p>"
            + "".join(f"<label style='font-weight:400'><input type='checkbox' name='models' "
                      f"value='{escape(m['id'])}'> {escape(m['name'])}</label>" for m in models)
        )
    inner = (
        f"{notice}<p>Workspace <strong>{escape(chosen_ws['name'])}</strong> &rarr; warehouse "
        f"<strong>{escape(chosen_wh['name'])}</strong>.</p>"
        f"<form method='post' action='{_BASE}/fabric'>"
        f"{_hidden(csrf=browser.csrf, flow=flow, workspace=workspace, warehouse=warehouse)}"
        "<p><strong>Which schemas may Claude read?</strong> Everything else in this warehouse "
        f"stays invisible to it.</p>{boxes}"
        f"{model_boxes}"
        f"{_name_fields(chosen_wh['name'])}"
        "<div class='row'><button class='primary' type='submit'>Create connection</button>"
        "</div></form>"
    )
    return _shell(request, browser, "New Fabric connection", 4, inner, flow)


@router.post(f"{_BASE}/fabric")
async def fabric_create(request: Request) -> Response:
    form = await request.form()
    fields: dict[str, Any] = {key: form.get(key, "") for key in
                              ("csrf", "flow", "workspace", "warehouse", "name", "notes")}
    chosen = [str(s) for s in form.getlist("schemas")]
    fields["models"] = [str(m) for m in form.getlist("models")]
    # The lookups below call Fabric and the database: keep them off the event loop.
    return await run_in_threadpool(_fabric_create, request, fields, chosen)


def _fabric_create(request: Request, fields: dict[str, Any], chosen: list[str]) -> Response:
    state = request.app.state
    flow = _flow(fields["flow"])
    browser = state.sessions.read(request)
    if browser is None or not browser.csrf_matches(fields["csrf"]):
        return message("Request refused", "Reload the page and try again.", 403)
    if state.entra is None:
        return message("Not available", "This server is not set up for Microsoft Fabric.", 404)

    def again(problem: str) -> Response:
        return fabric_steps(request, flow=flow, workspace=fields["workspace"],
                            warehouse=fields["warehouse"], problem=problem)

    if not chosen:
        return again("Pick at least one schema.")
    try:
        # Look everything up again as the person: only ids come from the form.
        api = _fabric_api(request, browser.user_id)
        warehouse = _find([w for w in api.warehouses(fields["workspace"])
                           if w.get("sql_endpoint")], fields["warehouse"])
        if warehouse is None:
            return message("Not found", "That warehouse isn't visible to you.", 404)
        real_schemas = _schemas(request, browser.user_id, warehouse)
        real_models = {m["id"]: m for m in _models(api, fields["workspace"])}
    except (NotLinked, CredentialRejected):
        return _link_prompt(request, browser, flow, "Sign in with Microsoft again.")
    except AdapterError as error:
        return message("Fabric problem", str(error), 502)
    if not set(chosen) <= set(real_schemas):
        return again("Pick schemas from the list.")
    if not set(fields["models"]) <= set(real_models):
        return again("Pick semantic models from the list.")
    picked_models = [real_models[m] for m in fields["models"]]

    credential_id = linked_microsoft_account(state.db, browser.user_id)
    with state.db.session() as session:
        problem = _check_name(session, browser.user_id, fields["name"], fields["notes"])
        if problem is None:
            connections.create(
                session, session.get(User, browser.user_id), platform="fabric",
                name=fields["name"].strip(), custom_prompt=fields["notes"].strip(),
                scope={"catalog": warehouse["name"], "schemas": chosen},
                settings={"workspace_id": fields["workspace"], "warehouse_id": warehouse["id"],
                          "sql_endpoint": warehouse["sql_endpoint"],
                          "semantic_models": picked_models},
                credential_id=credential_id,
            )
    if problem:
        return again(problem)
    return _finished(flow)


# --- Databricks ----------------------------------------------------------------------


def _databricks_sign_in(state):
    """
    (provider, token source, scope, label) for Databricks: its own OAuth when set up,
    otherwise -- on Azure -- the linked Microsoft account; None when neither is.
    """
    if not state.settings.databricks_enabled:
        return None
    if state.databricks_oauth is not None:
        return ("databricks", state.databricks_oauth, "all-apis",
                "your Databricks account (Databricks sign-in)")
    if state.entra is not None:
        return ("entra", state.entra, AZURE_DATABRICKS_SCOPE,
                "your Microsoft account (Azure Databricks)")
    return None


def _databricks_api(request: Request, user_id):
    state = request.app.state
    provider, tokens, scope, _ = _databricks_sign_in(state)
    _, token = provider_token(state.db, state.secrets, tokens, provider, user_id, scope)
    adapter = state.pipeline.registry.get("databricks")
    return adapter.api({"access_tokens": {scope: token}})


def _databricks_link_prompt(request, browser, flow: str, why: str = "") -> Response:
    back_here = _url(f"{_BASE}/databricks", flow)
    provider = _databricks_sign_in(request.app.state)[0]
    if provider == "entra":
        link = "/auth/platform/entra/start?" + urlencode({"next": back_here,
                                                          "platform": "databricks"})
    else:
        link = "/auth/platform/databricks/start?" + urlencode({"next": back_here})
    inner = (
        (f"<div class='notice bad' role='alert'>{escape(why)}</div>" if why else "")
        + "<p>IQ Connect reads Databricks <strong>as you</strong>, signed in on your "
        "workspace's own sign-in page, so Unity Catalog permissions apply exactly as they do "
        "to you -- and Claude only sees the schemas you pick next. No password or personal "
        "access token is stored.</p>"
        f"<div class='row'><a href='{escape(link)}'>Sign in with "
        f"{'Microsoft' if provider == 'entra' else 'Databricks'}</a></div>"
    )
    return _shell(request, browser, "New Databricks connection", 2, inner, flow)


def _links(items: list[tuple[str, str]], empty: str) -> str:
    return "<ul>" + ("".join(
        f"<li><a href='{escape(href)}'>{escape(label)}</a></li>" for label, href in items
    ) or f"<li class='empty'>{escape(empty)}</li>") + "</ul>"


@router.get(f"{_BASE}/databricks")
def databricks_steps(request: Request, flow: str = "", warehouse: str = "",
                     catalog: str = "", problem: str = "") -> Response:
    flow = _flow(flow)
    browser, redirect = _signed_in(request, flow)
    if redirect:
        return redirect
    state = request.app.state
    how = _databricks_sign_in(state)
    if how is None:
        return message("Not available", "This server is not set up for Databricks.", 404)
    if linked_account(state.db, browser.user_id, how[0]) is None:
        return _databricks_link_prompt(request, browser, flow)
    title = "New Databricks connection"
    here = f"{_BASE}/databricks"
    try:
        api = _databricks_api(request, browser.user_id)
        warehouses = api.warehouses()
        if not warehouse:
            choices = [(f"{w['name']} ({w['state'].lower() or 'unknown'})",
                        _url(here, flow, warehouse=w["id"])) for w in warehouses]
            return _shell(request, browser, title, 3,
                          "<p>Which SQL warehouse should run Claude's queries?</p>"
                          + _links(choices, "You can't use any SQL warehouse."), flow)
        chosen_wh = _find(warehouses, warehouse)
        if chosen_wh is None:
            return message("Not found", "That SQL warehouse isn't available to you.", 404)
        catalogs = api.catalogs()
        if not catalog:
            choices = [(c, _url(here, flow, warehouse=warehouse, catalog=c)) for c in catalogs]
            return _shell(request, browser, title, 3,
                          f"<p>Warehouse <strong>{escape(chosen_wh['name'])}</strong>. "
                          "Choose a catalog:</p>"
                          + _links(choices, "You can't see any catalogs."), flow)
        if catalog not in catalogs:
            return message("Not found", "That catalog isn't visible to you.", 404)
        schemas = api.schemas(catalog)
    except NotLinked:
        return _databricks_link_prompt(request, browser, flow)
    except CredentialRejected:
        return _databricks_link_prompt(request, browser, flow,
                                       "Databricks no longer accepts your sign-in. Sign in again.")
    except AdapterError as error:
        return _shell(request, browser, title, 3,
                      f"<div class='notice bad' role='alert'>{escape(str(error))}</div>", flow,
                      status_code=502)

    notice = f"<div class='notice bad' role='alert'>{escape(problem)}</div>" if problem else ""
    boxes = "".join(
        f"<label style='font-weight:400'><input type='checkbox' name='schemas' "
        f"value='{escape(s)}'> {escape(s)}</label>" for s in schemas
    ) or "<p class='empty'>No schemas you can read in this catalog.</p>"
    inner = (
        f"{notice}<p>Warehouse <strong>{escape(chosen_wh['name'])}</strong> &rarr; catalog "
        f"<strong>{escape(catalog)}</strong>.</p>"
        f"<form method='post' action='{here}'>"
        f"{_hidden(csrf=browser.csrf, flow=flow, warehouse=warehouse, catalog=catalog)}"
        "<p><strong>Which schemas may Claude read?</strong> Everything else stays invisible "
        f"to it.</p>{boxes}{_name_fields(catalog)}"
        "<div class='row'><button class='primary' type='submit'>Create connection</button>"
        "</div></form>"
    )
    return _shell(request, browser, title, 4, inner, flow)


@router.post(f"{_BASE}/databricks")
async def databricks_create(request: Request) -> Response:
    form = await request.form()
    fields: dict[str, Any] = {key: form.get(key, "") for key in
                              ("csrf", "flow", "warehouse", "catalog", "name", "notes")}
    chosen = [str(s) for s in form.getlist("schemas")]
    return await run_in_threadpool(_databricks_create, request, fields, chosen)


def _databricks_create(request: Request, fields: dict[str, Any], chosen: list[str]) -> Response:
    state = request.app.state
    flow = _flow(fields["flow"])
    browser = state.sessions.read(request)
    if browser is None or not browser.csrf_matches(fields["csrf"]):
        return message("Request refused", "Reload the page and try again.", 403)
    how = _databricks_sign_in(state)
    if how is None:
        return message("Not available", "This server is not set up for Databricks.", 404)

    def again(problem: str) -> Response:
        return databricks_steps(request, flow=flow, warehouse=fields["warehouse"],
                                catalog=fields["catalog"], problem=problem)

    if not chosen:
        return again("Pick at least one schema.")
    try:
        # Look everything up again as the person: only names and ids come from the form.
        api = _databricks_api(request, browser.user_id)
        if _find(api.warehouses(), fields["warehouse"]) is None:
            return message("Not found", "That SQL warehouse isn't available to you.", 404)
        if fields["catalog"] not in api.catalogs():
            return message("Not found", "That catalog isn't visible to you.", 404)
        real_schemas = api.schemas(fields["catalog"])
    except (NotLinked, CredentialRejected):
        return _databricks_link_prompt(request, browser, flow, "Sign in with Databricks again.")
    except AdapterError as error:
        return message("Databricks problem", str(error), 502)
    if not set(chosen) <= set(real_schemas):
        return again("Pick schemas from the list.")

    credential_id = linked_account(state.db, browser.user_id, how[0])
    with state.db.session() as session:
        problem = _check_name(session, browser.user_id, fields["name"], fields["notes"])
        if problem is None:
            connections.create(
                session, session.get(User, browser.user_id), platform="databricks",
                name=fields["name"].strip(), custom_prompt=fields["notes"].strip(),
                scope={"catalog": fields["catalog"], "schemas": chosen},
                settings={"warehouse_id": fields["warehouse"]},
                credential_id=credential_id,
            )
    if problem:
        return again(problem)
    return _finished(flow)
