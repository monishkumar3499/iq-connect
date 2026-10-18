"""
The dashboard at `/`: what you have connected, what Claude has been doing, and
the few changes that can be made here (rename, notes, delete, disconnect).

Every query is filtered by the signed-in person: nothing here can show or
change another person's connections, activity or apps. Every button is a form
post carrying the session's CSRF token. Outcomes come back as a fixed code in
the address (`?done=saved`), never as free text, so a crafted link can't put
words on this page.

Scope (catalog, schemas) is shown but not edited here: choosing schemas means
browsing the platform as the person, which belongs to the setup wizard (step 11).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import func, select, update

from ..connections import service as connections
from ..credentials.service import read_secret
from ..db.models import AuditEvent, Credential, DataConnection, OAuthClient, OAuthRefreshToken, User
from ..db.types import utcnow
from ..security.crypto import DecryptionError
from .pages import app_page, escape, message, page

router = APIRouter(tags=["dashboard"])

MAX_NAME = 100
MAX_NOTES = 2000

_DONE = {
    "saved": "Saved. Claude uses the new details from its next question.",
    "deleted": "Connection deleted.",
    "added": "Added the demo connection. Ask Claude about it now.",
    "disconnected": "Disconnected. Claude loses access within the hour, when its current "
                    "pass expires; it will ask you to connect again.",
    "unlinked": "Microsoft account removed from IQ Connect. Your Fabric connections now need "
                "you to sign in again. (To also withdraw the permission at Microsoft, remove "
                "the app from your Microsoft account's app permissions.)",
}
_PROBLEMS = {
    "name_empty": "A connection needs a name.",
    "name_long": f"Names can be at most {MAX_NAME} characters.",
    "name_taken": "You already have a connection with that name.",
    "notes_long": f"Notes can be at most {MAX_NOTES} characters.",
    "not_found": "That connection no longer exists.",
    "no_demo": "The demo platform is switched off on this server.",
}

_OUTCOME_PILL = {"ok": "ok", "rejected": "warn", "error": "bad"}
_PLATFORM_LABEL = {"demo": "Demo", "fabric": "Microsoft Fabric",
                   "databricks": "Databricks", "snowflake": "Snowflake"}


# --- helpers ---------------------------------------------------------------------


def _ago(moment) -> str:
    seconds = int((utcnow() - moment).total_seconds())
    if seconds < 60:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("min", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'s' if count != 1 and unit != 'min' else ''} ago"
    return "just now"


def _when(moment) -> str:
    stamp = moment.strftime("%Y-%m-%d %H:%M UTC")
    return f"<span title='{escape(stamp)}'>{escape(_ago(moment))}</span>"


def _csrf_field(csrf: str) -> str:
    return f"<input type='hidden' name='csrf' value='{escape(csrf)}'>"


def _back(done: str | None = None, problem: str | None = None) -> RedirectResponse:
    target = "/?done=" + done if done else "/?problem=" + (problem or "")
    return RedirectResponse(target, status_code=303)


def _require(request: Request, csrf: str):
    """The signed-in person for a form post, or the response refusing it."""
    browser = request.app.state.sessions.read(request)
    if browser is None:
        return None, message("Signed out", "Sign in again, then retry.", 401)
    if not browser.csrf_matches(csrf):
        return None, message("Request refused", "Reload the page and try again.", 403)
    return browser, None


# --- the page ---------------------------------------------------------------------


def _landing(request: Request) -> Response:
    settings = request.app.state.settings
    return page("IQ Connect", (
        "<h1>IQ Connect</h1>"
        "<p>Ask Claude questions about your Fabric, Databricks and Snowflake data -- "
        "read-only, and only the data you choose.</p>"
        "<p class='muted'>Add this connector in Claude with the address</p>"
        f"<span class='url'>{escape(settings.mcp_url)}</span>"
        "<div class='row'><a href='/auth/login?next=/'>Sign in to see your dashboard</a></div>"
    ))


@router.get("/")
def dashboard(request: Request, done: str = "", problem: str = "") -> Response:
    state = request.app.state
    browser = state.sessions.read(request)
    if browser is None:
        return _landing(request)

    week_ago = utcnow() - timedelta(days=7)
    with state.db.session() as session:
        user = session.get(User, browser.user_id)
        if user is None:
            response = _landing(request)
            state.sessions.end(response)
            return response
        email = user.email
        rows = connections.list_for_user(session, user.id)
        conns = [(c.id, c.name, c.platform, dict(c.scope), c.custom_prompt, c.status,
                  c.credential_id is not None) for c in rows]
        names = {c.id: c.name for c in rows}
        microsoft = _microsoft_account(state, session, user.id)

        stats = dict(session.execute(
            select(AuditEvent.outcome, func.count())
            .where(AuditEvent.user_id == user.id, AuditEvent.occurred_at >= week_ago)
            .group_by(AuditEvent.outcome)
        ).all())
        average_ms = session.scalar(
            select(func.avg(AuditEvent.duration_ms))
            .where(AuditEvent.user_id == user.id, AuditEvent.occurred_at >= week_ago,
                   AuditEvent.outcome == "ok")
        )
        recent = session.execute(
            select(AuditEvent.occurred_at, AuditEvent.tool, AuditEvent.connection_id,
                   AuditEvent.outcome, AuditEvent.row_count, AuditEvent.duration_ms,
                   AuditEvent.statement, AuditEvent.error)
            .where(AuditEvent.user_id == user.id)
            .order_by(AuditEvent.id.desc()).limit(30)
        ).all()
        apps = session.execute(
            select(OAuthRefreshToken.client_id, func.max(OAuthRefreshToken.created_at),
                   OAuthClient.name)
            .join(OAuthClient, OAuthClient.client_id == OAuthRefreshToken.client_id,
                  isouter=True)
            .where(OAuthRefreshToken.user_id == user.id,
                   OAuthRefreshToken.revoked_at.is_(None),
                   OAuthRefreshToken.expires_at > utcnow())
            .group_by(OAuthRefreshToken.client_id, OAuthClient.name)
        ).all()

    csrf = browser.csrf
    parts: list[str] = []
    if done in _DONE:
        parts.append(f"<div class='notice ok' role='status'>{escape(_DONE[done])}</div>")
    if problem in _PROBLEMS:
        parts.append(f"<div class='notice bad' role='alert'>{escape(_PROBLEMS[problem])}</div>")

    # --- summary tiles ---
    total = sum(stats.values())
    needs_signin = sum(1 for c in conns if c[5] != "active")
    tiles = [
        ("Connections", str(len(conns)),
         f"{needs_signin} need sign-in" if needs_signin else "all active"),
        ("Questions answered", str(stats.get("ok", 0)), "last 7 days"),
        ("Refused", str(stats.get("rejected", 0)), "outside scope or not read-only"),
        ("Avg. response", f"{average_ms / 1000:.1f} s" if average_ms else "--",
         f"{total} tool calls in 7 days"),
    ]
    parts.append("<div class='tiles'>" + "".join(
        f"<div class='tile'><div class='k'>{escape(k)}</div><div class='v'>{escape(v)}</div>"
        f"<div class='s'>{escape(s)}</div></div>" for k, v, s in tiles
    ) + "</div>")

    parts.append(_connections_panel(conns, csrf, state, microsoft))
    parts.append(_activity_panel(recent, names))
    parts.append(_setup_panel(state, csrf, apps, microsoft))
    return app_page("Dashboard", email=email, csrf=csrf, body_html="".join(parts))


def _connections_panel(conns, csrf: str, state, microsoft: str | None) -> str:
    if not conns:
        body = ("<p class='empty'>No connections yet. "
                "<a href='/app/connections/new'>Create your first connection</a>.</p>")
    else:
        cards = []
        for cid, name, platform, scope, notes, status, has_login in conns:
            pill = ("<span class='pill ok'>active</span>" if status == "active"
                    else "<span class='pill warn'>needs sign-in</span>")
            schemas = ", ".join(str(s) for s in scope.get("schemas", [])) or "none"
            shown_notes = escape(notes) if notes.strip() else "<span class=muted>none</span>"
            if platform == "demo":
                login = "not needed (built-in sample data)"
            elif has_login and platform == "fabric" and microsoft:
                login = f"Microsoft account {escape(microsoft)}"
            elif has_login:
                login = "linked"
            else:
                login = "<span class='pill warn'>none -- sign in again</span>"
            cards.append(
                "<div class='conn'>"
                f"<div class='conn-h'><strong>{escape(name)}</strong>"
                f"<span class='pill plain'>{escape(_PLATFORM_LABEL.get(platform, platform))}"
                f"</span>{pill}</div>"
                "<dl>"
                f"<dt>Database</dt><dd>{escape(str(scope.get('catalog', '')) or '--')}</dd>"
                f"<dt>Schemas</dt><dd>{escape(schemas)}</dd>"
                f"<dt>Notes for Claude</dt><dd>{shown_notes}</dd>"
                f"<dt>Sign-in</dt><dd>{login}</dd></dl>"
                f"<form method='post' action='/app/connections/{cid}/test' class='row' "
                f"style='margin-top:.6rem'>{_csrf_field(csrf)}"
                "<button type='submit'>Test connection</button></form>"
                "<details><summary>Edit name and notes</summary>"
                f"<form method='post' action='/app/connections/{cid}/edit'>{_csrf_field(csrf)}"
                f"<label for='n-{cid}'>Name</label>"
                f"<input id='n-{cid}' name='name' type='text' maxlength='{MAX_NAME}' required "
                f"value='{escape(name)}'>"
                f"<label for='p-{cid}'>Notes for Claude</label>"
                f"<textarea id='p-{cid}' name='notes' maxlength='{MAX_NOTES}'>"
                f"{escape(notes)}</textarea>"
                "<p class='muted'>Claude sees these notes with every answer from this "
                "connection: units, definitions, what to watch out for.</p>"
                "<div class='row'><button class='primary' type='submit'>Save</button></div>"
                "</form></details>"
                "<details><summary>Delete</summary>"
                f"<form method='post' action='/app/connections/{cid}/delete'>{_csrf_field(csrf)}"
                f"<p>Delete <strong>{escape(name)}</strong>? Claude will no longer be able to "
                "use it. Its past activity stays in the history.</p>"
                "<div class='row'><button class='danger' type='submit'>Delete connection"
                "</button></div></form></details>"
                "</div>"
            )
        body = "<div class='conns'>" + "".join(cards) + "</div>"
    return (
        "<section class='panel' aria-labelledby='h-conn'><div class='panel-h'>"
        "<h2 id='h-conn'>Your connections</h2>"
        "<p class='muted'>Claude can only use these, only read, and only inside each "
        "one's schemas.</p>"
        "<a href='/app/connections/new' style='margin-left:auto'>+ New connection</a></div>"
        f"<div class='panel-b'>{body}</div></section>"
    )


def _activity_panel(recent, names) -> str:
    if not recent:
        body = ("<p class='empty'>Nothing yet. Ask Claude a question about your data and "
                "it shows up here.</p>")
    else:
        lines = []
        for when, tool, cid, outcome, rows, ms, statement, error in recent:
            connection = names.get(cid, "(deleted)") if cid else "--"
            detail = statement or ""
            if error and outcome != "ok":
                detail = (detail + "\n" if detail else "") + "Reason: " + error
            lines.append(
                f"<tr><td class='nowrap'>{_when(when)}</td>"
                f"<td>{escape(tool.replace('_', ' '))}</td>"
                f"<td>{escape(connection)}</td>"
                f"<td><span class='pill {_OUTCOME_PILL.get(outcome, 'plain')}'>"
                f"{escape(outcome)}</span></td>"
                f"<td class='num'>{'' if rows is None else rows}</td>"
                f"<td class='num'>{ms / 1000:.2f} s</td>"
                f"<td><div class='sql'>{escape(detail[:600])}</div></td></tr>"
            )
        body = (
            "<div class='scroll'><table><thead><tr><th>When</th><th>Tool</th>"
            "<th>Connection</th><th>Result</th><th>Rows</th><th>Time</th>"
            "<th>Query / reason</th></tr></thead><tbody>" + "".join(lines) + "</tbody></table>"
            "</div>"
        )
    return (
        "<section class='panel' aria-labelledby='h-act'><div class='panel-h'>"
        "<h2 id='h-act'>Recent activity</h2>"
        "<p class='muted'>Every call Claude made for you, including refused ones. "
        "Latest 30.</p></div>"
        f"<div class='panel-b'>{body}</div></section>"
    )


def _setup_panel(state, csrf: str, apps, microsoft: str | None) -> str:
    settings = state.settings
    app_rows = []
    for client_id, last, name in apps:
        app_rows.append(
            f"<tr><td>{escape(name or 'Unnamed app')}</td><td class='nowrap'>{_when(last)}</td>"
            "<td><form method='post' action='/app/apps/disconnect'>"
            f"{_csrf_field(csrf)}<input type='hidden' name='client_id' value='{escape(client_id)}'>"
            "<button class='danger' type='submit'>Disconnect</button></form></td></tr>"
        )
    apps_html = (
        "<table><thead><tr><th>App</th><th>Last connected</th><th></th></tr></thead><tbody>"
        + "".join(app_rows) + "</tbody></table>"
        if app_rows else "<p class='empty'>No app is connected yet.</p>"
    )

    steps = [
        "In Claude: Settings &rarr; Connectors &rarr; Add custom connector, with the "
        "address below, then Connect and Allow."
        f"<span class='url'>{escape(settings.mcp_url)}</span>",
    ]
    if state.entra is not None:
        if microsoft:
            steps.append(
                f"Microsoft account for Fabric: <strong>{escape(microsoft)}</strong> &middot; "
                "<a href='/auth/platform/entra/start?next=/'>Sign in with a different account"
                "</a> &middot; <form method='post' action='/app/microsoft/unlink' "
                f"style='display:inline'>{_csrf_field(csrf)}"
                "<button class='danger' type='submit'>Remove sign-in</button></form>"
            )
        else:
            steps.append(
                "Microsoft account for Fabric: <strong>not linked</strong> &middot; "
                "<a href='/auth/platform/entra/start?next=/'>Sign in with Microsoft</a>"
            )
    if settings.demo_enabled:
        steps.append(
            "Try it with sample data: <form method='post' action='/app/demo-connection' "
            f"style='display:inline'>{_csrf_field(csrf)}"
            "<button type='submit'>Add the demo connection</button></form>"
        )
    return (
        "<section class='panel' aria-labelledby='h-setup'><div class='panel-h'>"
        "<h2 id='h-setup'>Connected apps and setup</h2></div><div class='panel-b'>"
        f"{apps_html}<h2 style='margin-top:1.25rem'>Set up</h2>"
        "<ol class='steps'>" + "".join(f"<li>{s}</li>" for s in steps) + "</ol>"
        "</div></section>"
    )


# --- actions -----------------------------------------------------------------------


def _own_connection(session, user_id: uuid.UUID, connection_id: str) -> DataConnection | None:
    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return None
    row = session.get(DataConnection, cid)
    return row if row is not None and row.user_id == user_id else None


@router.post("/app/connections/{connection_id}/edit")
def edit_connection(request: Request, connection_id: str, name: str = Form(""),
                    notes: str = Form(""), csrf: str = Form("")) -> Response:
    browser, refusal = _require(request, csrf)
    if refusal:
        return refusal
    name = name.strip()
    if not name:
        return _back(problem="name_empty")
    if len(name) > MAX_NAME:
        return _back(problem="name_long")
    if len(notes) > MAX_NOTES:
        return _back(problem="notes_long")
    with request.app.state.db.session() as session:
        row = _own_connection(session, browser.user_id, connection_id)
        if row is None:
            return _back(problem="not_found")
        clash = session.scalars(select(DataConnection.id).where(
            DataConnection.user_id == browser.user_id, DataConnection.id != row.id,
            func.lower(DataConnection.name) == name.lower(),
        )).first()
        if clash is not None:
            return _back(problem="name_taken")
        row.name, row.custom_prompt = name, notes.strip()
    return _back(done="saved")


@router.post("/app/connections/{connection_id}/delete")
def delete_connection(request: Request, connection_id: str, csrf: str = Form("")) -> Response:
    browser, refusal = _require(request, csrf)
    if refusal:
        return refusal
    with request.app.state.db.session() as session:
        row = _own_connection(session, browser.user_id, connection_id)
        if row is None:
            return _back(problem="not_found")
        session.delete(row)
    return _back(done="deleted")


@router.post("/app/demo-connection")
def add_demo(request: Request, csrf: str = Form("")) -> Response:
    browser, refusal = _require(request, csrf)
    if refusal:
        return refusal
    if not request.app.state.settings.demo_enabled:
        return _back(problem="no_demo")
    with request.app.state.db.session() as session:
        user = session.get(User, browser.user_id)
        existing = {c.name.lower() for c in connections.list_for_user(session, user.id)}
        name = "Demo sales"
        number = 2
        while name.lower() in existing:
            name, number = f"Demo sales {number}", number + 1
        connections.create(
            session, user, platform="demo", name=name,
            scope={"catalog": "demo", "schemas": ["sales"]},
            custom_prompt="Sample data for trying IQ Connect. Amounts are in USD.",
        )
    return _back(done="added")


@router.post("/app/apps/disconnect")
def disconnect_app(request: Request, client_id: str = Form(""),
                   csrf: str = Form("")) -> Response:
    browser, refusal = _require(request, csrf)
    if refusal:
        return refusal
    with request.app.state.db.session() as session:
        session.execute(
            update(OAuthRefreshToken)
            .where(OAuthRefreshToken.user_id == browser.user_id,
                   OAuthRefreshToken.client_id == client_id,
                   OAuthRefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
    return _back(done="disconnected")


# --- sign-in (credentials) ---------------------------------------------------------------


def _microsoft_account(state, session, user_id: uuid.UUID) -> str | None:
    """The linked Microsoft account's username, "" if unreadable, None if none is linked."""
    credential = session.scalars(
        select(Credential).where(Credential.user_id == user_id, Credential.provider == "entra")
    ).first()
    if credential is None:
        return None
    try:
        return str(read_secret(state.secrets, credential).get("username") or "linked")
    except DecryptionError:
        return "unreadable -- sign in again"


@router.post("/app/microsoft/unlink")
def unlink_microsoft(request: Request, csrf: str = Form("")) -> Response:
    browser, refusal = _require(request, csrf)
    if refusal:
        return refusal
    with request.app.state.db.session() as session:
        for credential in session.scalars(select(Credential).where(
            Credential.user_id == browser.user_id, Credential.provider == "entra",
        )).all():
            # Connections keep their name, scope and notes; they wait for a new
            # sign-in, and linking again reattaches them automatically.
            session.execute(
                update(DataConnection)
                .where(DataConnection.credential_id == credential.id)
                .values(status="needs_signin", credential_id=None)
            )
            session.delete(credential)
    return _back(done="unlinked")


# --- test connection ------------------------------------------------------------------------


@router.post("/app/connections/{connection_id}/test")
async def test_connection(request: Request, connection_id: str,
                          csrf: str = Form("")) -> Response:
    browser, refusal = _require(request, csrf)
    if refusal:
        return refusal
    state = request.app.state

    def lookup():
        with state.db.session() as session:
            row = _own_connection(session, browser.user_id, connection_id)
            if row is None:
                return None
            return row.id, row.name, row.platform, [str(s) for s in row.scope.get("schemas", [])]

    found = await asyncio.to_thread(lookup)
    if found is None:
        return _back(problem="not_found")
    cid, name, platform, schemas = found

    started = time.perf_counter()
    try:
        # The same path as a tool call from Claude -- connection, policy, sign-in,
        # platform, audit -- so a passing test means Claude's calls will work too.
        outcome = await state.pipeline.run(
            "test_connection", name, user_id=browser.user_id, allow_needs_signin=True,
            work=lambda adapter, ctx, secret: adapter.list_objects(ctx, secret, None),
        )
    except ToolError as error:
        status = await asyncio.to_thread(_status_of, state, cid)
        return _test_page(name, platform, ok=False, lines=[str(error)],
                          relink=status == "needs_signin" and platform == "fabric")
    seconds = time.perf_counter() - started

    found_in = {s.casefold(): 0 for s in schemas}
    for obj in outcome.value:
        if obj.schema.casefold() in found_in:
            found_in[obj.schema.casefold()] += 1
    total = sum(found_in.values())
    lines = [f"Signed in and connected in {seconds:.1f} s.",
             f"{total} tables and views Claude can read in {', '.join(schemas) or 'no schemas'}."]
    empty = [s for s in schemas if found_in[s.casefold()] == 0]
    if empty:
        lines.append("No readable tables in: " + ", ".join(empty)
                     + ". The schema may be empty, renamed, or not shared with you.")
    await asyncio.to_thread(_mark_active, state, cid)
    return _test_page(name, platform, ok=not empty, warn=bool(empty), lines=lines)


def _status_of(state, connection_id: uuid.UUID) -> str | None:
    with state.db.session() as session:
        row = session.get(DataConnection, connection_id)
        return row.status if row else None


def _mark_active(state, connection_id: uuid.UUID) -> None:
    # A test that signed in successfully proves the login works again.
    with state.db.session() as session:
        row = session.get(DataConnection, connection_id)
        if row is not None and (row.credential_id is not None or row.platform == "demo"):
            row.status = "active"


def _test_page(name: str, platform: str, *, ok: bool, lines: list[str],
               warn: bool = False, relink: bool = False) -> Response:
    if ok:
        pill = "<span class='pill ok'>working</span>"
    elif warn:
        pill = "<span class='pill warn'>working, with a warning</span>"
    else:
        pill = "<span class='pill bad'>not working</span>"
    actions = ["<a href='/'>Back to dashboard</a>"]
    if relink:
        link = "/auth/platform/entra/start?" + urlencode({"next": "/"})
        actions.insert(0, f"<a href='{escape(link)}'>Sign in with Microsoft again</a>")
    return page(f"Test: {name}", (
        f"<h1>Test: {escape(name)}</h1>"
        f"<p>{escape(_PLATFORM_LABEL.get(platform, platform))} &middot; {pill}</p>"
        + "".join(f"<p>{escape(line)}</p>" for line in lines)
        + "<div class='row'>" + " &middot; ".join(actions) + "</div>"
    ))
