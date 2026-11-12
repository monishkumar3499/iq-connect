"""
The person's own pages (dashboard, settings) and the server's one-time setup, as JSON.

  GET    /api/dashboard                     connections, activity, connected apps, totals
  PATCH  /api/connections/{id}              rename, notes      DELETE: remove it
  POST   /api/connections/{id}/test         a real read-only call through the tool pipeline
  PUT    /api/instructions                  the person's standing instructions for Claude
  POST   /api/apps/disconnect               revoke an app's (Claude's) refresh tokens
  POST   /api/microsoft/unlink              forget the linked Microsoft account
  GET    /api/admin/microsoft               the Microsoft app registration (POST: save)
  POST   /api/logout                        GET /api/flash: a message for the error page

Every query is filtered by the signed-in person: nothing here can show or change
another person's connections, activity or apps.

Who may save an app registration (credentials/app_store.py::may_change): an IQ
admin, or whoever has the setup code, or -- only where the code isn't required and
nobody has claimed it yet -- anyone. 20 wrong codes in 15 minutes pause code checks
on this copy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import func, select, update
from starlette.concurrency import run_in_threadpool

from backend.core.auth.users import MAX_INSTRUCTIONS, set_instructions
from backend.core.configuration.app_registrations import AppRejected, code_matches, may_change
from backend.core.connection_manager import connections
from backend.core.connection_manager.credentials import linked_usernames, read_secret
from backend.core.security.audit import record
from backend.core.security.crypto import DecryptionError
from backend.core.web.common import ApiError, change, is_admin, json_body, parse_uuid, person, text
from backend.shared.database import utcnow
from backend.shared.models import (
    AuditEvent,
    Credential,
    DataConnection,
    OAuthClient,
    OAuthRefreshToken,
    Org,
    SetupItem,
    User,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["account"])

MAX_NAME = 100
MAX_NOTES = 2000
# Wrong setup codes allowed per copy of the service in the window before pausing.
MAX_WRONG_CODES = 20
WRONG_CODE_WINDOW = 15 * 60


# --- the dashboard ------------------------------------------------------------------------------


@router.get("/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    browser = person(request)
    state = request.app.state
    week_ago = utcnow() - timedelta(days=7)
    with state.db.session() as session:
        user = session.get(User, browser.user_id)
        if user is None:
            raise ApiError(401, "signed_out", "Sign in to continue.")
        rows = connections.list_for_user(session, user.id)
        names = {c.id: c.name for c in rows}
        accounts = linked_usernames(session, state.secrets, user.id)
        picks = {i.connection_id: i for i in session.scalars(select(SetupItem).where(
            SetupItem.user_id == user.id, SetupItem.connection_id.is_not(None)))}
        stats = dict(session.execute(
            select(AuditEvent.outcome, func.count())
            .where(AuditEvent.user_id == user.id, AuditEvent.occurred_at >= week_ago)
            .group_by(AuditEvent.outcome)).all())
        average_ms = session.scalar(
            select(func.avg(AuditEvent.duration_ms))
            .where(AuditEvent.user_id == user.id, AuditEvent.occurred_at >= week_ago,
                   AuditEvent.outcome == "ok"))
        recent = session.execute(
            select(AuditEvent.occurred_at, AuditEvent.tool, AuditEvent.connection_id,
                   AuditEvent.outcome, AuditEvent.row_count, AuditEvent.duration_ms,
                   AuditEvent.statement, AuditEvent.error)
            .where(AuditEvent.user_id == user.id)
            .order_by(AuditEvent.id.desc()).limit(30)).all()
        apps = session.execute(
            select(OAuthRefreshToken.client_id, func.max(OAuthRefreshToken.created_at),
                   OAuthClient.name)
            .join(OAuthClient, OAuthClient.client_id == OAuthRefreshToken.client_id,
                  isouter=True)
            .where(OAuthRefreshToken.user_id == user.id,
                   OAuthRefreshToken.revoked_at.is_(None),
                   OAuthRefreshToken.expires_at > utcnow())
            .group_by(OAuthRefreshToken.client_id, OAuthClient.name)).all()
        return {
            "email": user.email,
            "instructions": user.instructions, "maxInstructions": MAX_INSTRUCTIONS,
            "microsoftAccount": _microsoft_account(state, session, user.id),
            "stats": {"answered": stats.get("ok", 0), "refused": stats.get("rejected", 0),
                      "calls": sum(stats.values()),
                      "averageSeconds": round(average_ms / 1000, 1) if average_ms else None},
            "connections": [_connection(c, accounts, picks.get(c.id)) for c in rows],
            "activity": [
                {"at": a.occurred_at.isoformat(), "tool": a.tool,
                 "connection": names.get(a.connection_id, ""), "outcome": a.outcome,
                 "rows": a.row_count, "ms": a.duration_ms,
                 "statement": (a.statement or "")[:400], "error": (a.error or "")[:300]}
                for a in recent],
            "apps": [{"clientId": client_id, "name": name or "An application",
                      "since": since.isoformat() if since else None}
                     for client_id, since, name in apps],
        }


def _connection(row: DataConnection, accounts: dict, pick: SetupItem | None) -> dict[str, Any]:
    model = connections.is_model(row)
    return {
        "id": str(row.id), "name": row.name, "platform": row.platform,
        "kind": "model" if model else "warehouse",
        "catalog": str((row.scope or {}).get("catalog", "")),
        "schemas": [str(s) for s in (row.scope or {}).get("schemas", [])],
        "models": [m.get("name", "") for m in (row.settings or {}).get("semantic_models", [])],
        "notes": row.custom_prompt, "status": row.status,
        # Which account it runs as: people use the same company account for us and the
        # platforms, and must be able to tell which one each connection signs in with.
        "account": accounts.get(row.credential_id, ""),
        "workspace": pick.workspace_name if pick else "",
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def _microsoft_account(state, session, user_id) -> str | None:
    """The linked Microsoft account's username, None if none is linked."""
    credential = session.scalars(select(Credential).where(
        Credential.user_id == user_id, Credential.provider == "entra")).first()
    if credential is None:
        return None
    try:
        return str(read_secret(state.secrets, credential).get("username") or "linked")
    except DecryptionError:
        return "unreadable -- sign in again"


def _own(session, user_id, connection_id: str) -> DataConnection:
    cid = parse_uuid(connection_id)
    row = session.get(DataConnection, cid) if cid else None
    if row is None or row.user_id != user_id:
        raise ApiError(404, "not_found", "That connection no longer exists.")
    return row


@router.patch("/connections/{connection_id}")
async def edit_connection(request: Request, connection_id: str) -> dict[str, Any]:
    browser, data = await change(request)
    name, notes = text(data, "name", MAX_NAME + 1).strip(), text(data, "notes", MAX_NOTES + 1)
    if not name:
        raise ApiError(400, "name_empty", "A connection needs a name.")
    if len(name) > MAX_NAME:
        raise ApiError(400, "name_long", f"Names can be at most {MAX_NAME} characters.")
    if len(notes) > MAX_NOTES:
        raise ApiError(400, "notes_long", f"Notes can be at most {MAX_NOTES} characters.")
    with request.app.state.db.session() as session:
        row = _own(session, browser.user_id, connection_id)
        clash = session.scalars(select(DataConnection.id).where(
            DataConnection.user_id == browser.user_id, DataConnection.id != row.id,
            func.lower(DataConnection.name) == name.lower())).first()
        if clash is not None:
            raise ApiError(409, "name_taken", "You already have a connection with that name.")
        row.name, row.custom_prompt = name, notes.strip()
    return {"ok": True}


@router.delete("/connections/{connection_id}")
def delete_connection(request: Request, connection_id: str) -> dict[str, Any]:
    browser = person(request)
    if not browser.csrf_matches(request.headers.get("x-csrf-token")):
        raise ApiError(403, "csrf", "Reload the page and try again.")
    with request.app.state.db.session() as session:
        session.delete(_own(session, browser.user_id, connection_id))
    return {"ok": True}


@router.put("/instructions")
async def save_instructions(request: Request) -> dict[str, Any]:
    browser, data = await change(request)
    with request.app.state.db.session() as session:
        if not set_instructions(session, browser.user_id, text(data, "text", 10**6)):
            raise ApiError(400, "too_long",
                           f"Instructions can be at most {MAX_INSTRUCTIONS:,} characters.")
    return {"ok": True}


@router.post("/apps/disconnect")
async def disconnect_app(request: Request) -> dict[str, Any]:
    browser, data = await change(request)
    with request.app.state.db.session() as session:
        session.execute(update(OAuthRefreshToken).where(
            OAuthRefreshToken.user_id == browser.user_id,
            OAuthRefreshToken.client_id == text(data, "clientId", 2000),
            OAuthRefreshToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
    return {"ok": True}


@router.post("/microsoft/unlink")
async def unlink_microsoft(request: Request) -> dict[str, Any]:
    browser, _ = await change(request)
    with request.app.state.db.session() as session:
        for credential in session.scalars(select(Credential).where(
                Credential.user_id == browser.user_id, Credential.provider == "entra")).all():
            # Connections keep their name, scope and notes and wait for a new sign-in.
            # Known gap: linking again does NOT reattach them (README section 20).
            session.execute(update(DataConnection)
                            .where(DataConnection.credential_id == credential.id)
                            .values(status="needs_signin", credential_id=None))
            session.delete(credential)
    return {"ok": True}


@router.post("/connections/{connection_id}/test")
async def test_connection(request: Request, connection_id: str) -> dict[str, Any]:
    browser, _ = await change(request)
    state = request.app.state

    def lookup():
        with state.db.session() as session:
            row = _own(session, browser.user_id, connection_id)
            return row.id, row.name, row.platform, [str(s) for s in row.scope.get("schemas", [])]

    cid, name, platform, schemas = await asyncio.to_thread(lookup)
    started = time.perf_counter()
    try:
        # The same path as a tool call from Claude -- connection, policy, sign-in,
        # platform, audit -- so a passing test means Claude's calls will work too.
        outcome = await state.pipeline.run(
            "test_connection", name, user_id=browser.user_id, allow_needs_signin=True,
            work=lambda adapter, ctx, secret: adapter.list_objects(ctx, secret, None))
    except ToolError as error:
        relink = platform == "fabric" and await asyncio.to_thread(
            _status_of, state, cid) == "needs_signin"
        return {"ok": False, "warn": False, "lines": [str(error)], "relink": relink}
    seconds = time.perf_counter() - started
    found_in = {s.casefold(): 0 for s in schemas}
    for obj in outcome.value:
        if obj.schema.casefold() in found_in:
            found_in[obj.schema.casefold()] += 1
    lines = [f"Signed in and connected in {seconds:.1f} s.",
             f"{sum(found_in.values())} tables and views Claude can read in "
             f"{', '.join(schemas) or 'no schemas'}."]
    empty = [s for s in schemas if found_in[s.casefold()] == 0]
    if empty:
        lines.append("No readable tables in: " + ", ".join(empty)
                     + ". The schema may be empty, renamed, or not shared with you.")
    await asyncio.to_thread(_mark_active, state, cid)
    return {"ok": not empty, "warn": bool(empty), "lines": lines, "relink": False}


def _status_of(state, connection_id) -> str | None:
    with state.db.session() as session:
        row = session.get(DataConnection, connection_id)
        return row.status if row else None


def _mark_active(state, connection_id) -> None:
    # A test that signed in successfully proves the login works again.
    with state.db.session() as session:
        row = session.get(DataConnection, connection_id)
        if row is not None and row.credential_id is not None:
            row.status = "active"


# --- the account itself (Settings) ----------------------------------------------------------------

PROVIDER_LABELS = {"entra": "Microsoft", "databricks": "Databricks", "snowflake": "Snowflake",
                   "fabric_sp": "Fabric service principal"}


@router.get("/account")
def account(request: Request) -> dict[str, Any]:
    browser = person(request)
    state = request.app.state
    with state.db.session() as session:
        user = session.get(User, browser.user_id)
        if user is None:
            raise ApiError(401, "signed_out", "Sign in to continue.")
        org = session.get(Org, user.org_id)
        usernames = linked_usernames(session, state.secrets, user.id)
        used = dict(session.execute(
            select(DataConnection.credential_id, func.count())
            .where(DataConnection.user_id == user.id)
            .group_by(DataConnection.credential_id)).all())
        logins = session.scalars(select(Credential).where(Credential.user_id == user.id)
                                 .order_by(Credential.created_at)).all()
        return {
            "id": str(user.id), "name": user.display_name, "email": user.email,
            "organization": org.name if org else "", "isAdmin": user.is_admin,
            "signInMethod": "Microsoft (single sign-on)" if state.identity.name == "entra"
            else "Development sign-in",
            "createdAt": user.created_at.isoformat() if user.created_at else None,
            "lastSignIn": user.last_login_at.isoformat() if user.last_login_at else None,
            "signIns": [{"provider": c.provider,
                         "label": PROVIDER_LABELS.get(c.provider, c.provider),
                         "account": usernames.get(c.id, ""), "connections": used.get(c.id, 0),
                         "since": c.created_at.isoformat() if c.created_at else None}
                        for c in logins],
        }


@router.post("/account/delete")
async def delete_account(request: Request) -> JSONResponse:
    browser, data = await change(request)
    state = request.app.state
    with state.db.session() as session:
        user = session.get(User, browser.user_id)
        if user is None:
            raise ApiError(401, "signed_out", "Sign in to continue.")
        # Typing the email is the person's explicit "yes": nothing here can be undone.
        if text(data, "confirm", 400).strip().casefold() != user.email.casefold():
            raise ApiError(400, "confirm", "Type your email address exactly as shown to "
                                           "confirm.")
        org_id = user.org_id
        # The database removes the rest with the person: connections, platform sign-ins,
        # setup picks and Claude's refresh tokens (so Claude loses access at once).
        session.delete(user)
    # Audit rows are deliberately kept (README section 9); this one says why the
    # person's id stops appearing.
    record(state.db, org_id=org_id, user_id=browser.user_id, tool="account_deleted",
           outcome="ok", duration_ms=0)
    log.info("User %s deleted their account", browser.user_id)
    response = JSONResponse({"redirect": "/setup"})
    state.sessions.end(response)
    return response


# --- sign-out and the error page's message --------------------------------------------------------


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    # The CSRF token stops another site from signing people out behind their backs.
    await change(request)
    response = JSONResponse({"redirect": "/setup"})
    request.app.state.sessions.end(response)
    return response


@router.get("/flash")
def flash(request: Request) -> JSONResponse:
    sessions = request.app.state.sessions
    response = JSONResponse(sessions.read_flash(request) or {})
    sessions.clear_flash(response)
    return response


# --- the app registrations (one-time server setup) -----------------------------------------------


def _owned(request: Request, exists: bool) -> bool:
    """
    A saved registration is owned once an IQ admin exists (whoever saved it signed
    in, or the first Microsoft sign-in). Until then, where the setup code isn't
    required, anyone may still correct it.
    """
    if not exists:
        return False
    with request.app.state.db.session() as session:
        return session.scalars(select(User.id).where(User.is_admin.is_(True))).first() \
            is not None


def _code_needed(request: Request, *, admin: bool, claimed: bool) -> bool:
    return not may_change(is_admin=admin, code_ok=False, claimed=claimed,
                          code_required=request.app.state.settings.requires_setup_code)


def _refusal(request: Request, *, admin: bool, claimed: bool, given_code: str) -> ApiError | None:
    state = request.app.state
    required = state.settings.requires_setup_code
    if may_change(is_admin=admin, code_ok=False, claimed=claimed, code_required=required):
        return None
    now = time.monotonic()
    state.setup_code_failures = [t for t in getattr(state, "setup_code_failures", [])
                                 if now - t < WRONG_CODE_WINDOW]
    if len(state.setup_code_failures) >= MAX_WRONG_CODES:
        return ApiError(429, "paused", "Too many wrong setup codes. Wait 15 minutes, then "
                                       "try again.")
    if code_matches(state.setup_code, given_code):
        return None
    state.setup_code_failures.append(now)
    if claimed and not required and not given_code:
        return ApiError(403, "claimed", "This is already set up. Only an IQ admin can change "
                                        "it (or use the setup code).")
    return ApiError(403, "bad_code", "That setup code isn't right. Copy it from the server "
                                     "window.")


async def _admin_change(request: Request, *, signed_in_only: bool):
    """(browser or None, body) for saving a registration: CSRF whenever signed in."""
    browser = request.app.state.sessions.read(request)
    if browser is None and signed_in_only:
        raise ApiError(401, "signed_out", "Sign in to continue.")
    if browser is not None and not browser.csrf_matches(request.headers.get("x-csrf-token")):
        raise ApiError(403, "csrf", "Reload the page and try again.")
    return browser, await json_body(request)


def _made_admin(state, browser, tool: str, statement: str) -> None:
    """Whoever saved while signed in may change it later without the code; audited."""
    if browser is None:
        return
    with state.db.session() as session:
        user = session.get(User, browser.user_id)
        if user is None:
            return
        user.is_admin = True
        org_id = user.org_id
    record(state.db, org_id=org_id, user_id=browser.user_id, tool=tool, outcome="ok",
           duration_ms=0, statement=statement)


@router.get("/admin/microsoft")
def microsoft_info(request: Request) -> dict[str, Any]:
    state = request.app.state
    browser = state.sessions.read(request)
    admin = is_admin(request, browser)
    found = state.microsoft.status()
    base = state.settings.public_base_url
    return {
        "status": None if found is None else {
            "clientId": found.client_id, "tenant": found.tenant, "source": found.source},
        "needsCode": _code_needed(request, admin=admin, claimed=_owned(request, found is not None)),
        "redirectUris": [base + "/auth/callback/entra", base + "/auth/platform/entra/callback"],
    }


@router.post("/admin/microsoft")
async def save_microsoft(request: Request) -> dict[str, Any]:
    browser, data = await _admin_change(request, signed_in_only=False)
    return await run_in_threadpool(_save_microsoft, request, browser, data)


def _save_microsoft(request: Request, browser, data: dict[str, Any]) -> dict[str, Any]:
    state = request.app.state
    found = state.microsoft.status()
    if found is not None and found.source == "settings":
        raise ApiError(409, "in_settings", "This server's Microsoft app is set in .env and can "
                                           "only be changed there.")
    refused = _refusal(request, admin=is_admin(request, browser),
                       claimed=_owned(request, found is not None),
                       given_code=text(data, "setupCode"))
    if refused:
        log.warning("Microsoft app setup refused: %s", refused.code)
        raise refused
    try:
        saved = state.microsoft.save(
            tenant=text(data, "tenant"), client_id=text(data, "clientId"),
            client_secret=text(data, "clientSecret", 2000),
            user_id=browser.user_id if browser else None)
    except AppRejected as error:
        raise ApiError(400, "rejected", str(error)) from error
    _made_admin(state, browser, "microsoft_app_saved", f"client {saved.client_id}")
    return {"ok": True}
