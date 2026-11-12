"""
The onboarding wizard's engine: turn each resource a person picked into a
saved, working connection. What each platform lists and how it checks a
resource lives in `setup/platforms/`.

One connection per resource, so every tool call keeps naming exactly one:
  a SQL warehouse     -> a connection scoped to the schemas chosen for it
  a semantic model    -> a connection holding just that model (Fabric only),
                         so a model is never tied to a warehouse it has
                         nothing to do with

Saving a resource never trusts the browser beyond ids. `resolve` looks the
workspace, the resource and the schemas up again as the person, then probes
the resource with a real read-only call before anything is stored. Only then
does the item turn "connected". A failure is kept on its SetupItem with a
sentence the person can act on, and leaves every other item untouched.

The same resource picked again finds its SetupItem (unique per person) and
its connection (matched by platform resource id, see `_identity`), so
repeating setup updates rather than duplicates.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update

from backend.core.configuration.observability import REQUEST_ID
from backend.core.connection_manager import connections
from backend.core.connection_manager.credentials import NotLinked
from backend.core.connection_manager.drivers import (
    MODEL,
    Resource,
    SetupProblem,
    Spec,
    platform_for,
)
from backend.core.dynamic_registry.base import AdapterError, CredentialRejected
from backend.core.security.audit import record
from backend.shared.database import utcnow
from backend.shared.models import DataConnection, SetupItem, User

log = logging.getLogger(__name__)

# A "connecting" item older than this was abandoned (the copy handling it
# stopped); it may be claimed again.
STALE_CONNECTING = timedelta(minutes=5)
MAX_ERROR = 300


def _explain(error: Exception) -> str:
    text = str(error).strip() or type(error).__name__
    return text[:MAX_ERROR]


# --- saving --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Choice:
    kind: str
    resource: Resource
    catalog: str = ""
    schemas: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        base = f"{'model' if self.kind == MODEL else 'warehouse'}:{self.resource.id}"
        return f"{base}:{self.catalog}" if self.catalog else base


def record_choices(db: Any, user_id: uuid.UUID, platform: str, workspace: Resource,
                   choices: list[Choice]) -> list[uuid.UUID]:
    """
    Save the picks as pending SetupItems -- updating the person's existing item
    for the same resource -- and return their ids. An item another request is
    connecting right now is left alone (a double-clicked submit).
    """
    ids: list[uuid.UUID] = []
    with db.session() as session:
        for choice in choices:
            item = session.scalars(select(SetupItem).where(
                SetupItem.user_id == user_id, SetupItem.platform == platform,
                SetupItem.workspace_id == workspace.id,
                SetupItem.resource_key == choice.key)).one_or_none()
            if item is None:
                item = SetupItem(user_id=user_id, platform=platform, workspace_id=workspace.id,
                                 resource_key=choice.key, kind=choice.kind,
                                 resource_id=choice.resource.id)
                session.add(item)
            elif item.status == "connecting" and not _stale(item):
                ids.append(item.id)
                continue
            item.workspace_name = workspace.name[:300]
            item.resource_name = choice.resource.name[:300]
            item.catalog = choice.catalog
            item.schemas = list(choice.schemas)
            item.status = "pending"
            item.error = ""
            session.flush()
            ids.append(item.id)
    return ids


def _stale(item: SetupItem) -> bool:
    return item.updated_at is not None and item.updated_at < utcnow() - STALE_CONNECTING


def connect_item(state: Any, user_id: uuid.UUID, item_id: uuid.UUID) -> str:
    """
    Validate one item as the person and save its connection. Returns the item's
    final status. Claiming the item is one conditional UPDATE, so two requests
    (two copies of the service, a double submit) never connect it twice.
    """
    db = state.db
    with db.session() as session:
        claimed = session.execute(
            update(SetupItem)
            .where(SetupItem.id == item_id, SetupItem.user_id == user_id,
                   or_(SetupItem.status.in_(("pending", "failed", "connected")),
                       SetupItem.updated_at < utcnow() - STALE_CONNECTING))
            .values(status="connecting", error="", updated_at=utcnow())
        ).rowcount
    if not claimed:
        return "connecting"

    started = time.perf_counter()
    with db.session() as session:
        item = session.get(SetupItem, item_id)
        user = session.get(User, user_id)
        org_id = user.org_id
        session.expunge(item)
    platform = platform_for(state, item.platform, user_id)
    connection_id: uuid.UUID | None = None
    try:
        if platform is None:
            raise SetupProblem("This platform is not available on this server.")
        spec = platform.resolve(item)
        with db.session() as session:
            connection_id = _save_connection(session, session.get(User, user_id), item.platform,
                                             spec)
            session.execute(update(SetupItem).where(SetupItem.id == item_id).values(
                status="connected", error="", connection_id=connection_id, updated_at=utcnow()))
        outcome, error = "connected", ""
    except NotLinked:
        outcome, error = "failed", "You're not signed in to this platform. Sign in, then retry."
    except CredentialRejected as problem:
        outcome, error = "failed", f"{_explain(problem)} Sign in again, then retry."
    except (SetupProblem, AdapterError) as problem:
        outcome, error = "failed", _explain(problem)
    except Exception:
        reference = REQUEST_ID.get()
        log.exception("Setting up %s item %s failed", item.platform, item_id)
        outcome = "failed"
        error = f"Something unexpected went wrong (reference {reference}). Try again."
    if outcome == "failed":
        with db.session() as session:
            session.execute(update(SetupItem).where(SetupItem.id == item_id).values(
                status="failed", error=error[:MAX_ERROR], updated_at=utcnow()))
    record(db, org_id=org_id, user_id=user_id, tool="setup_resource",
           outcome="ok" if outcome == "connected" else "error",
           duration_ms=int((time.perf_counter() - started) * 1000),
           connection_id=connection_id, platform=item.platform,
           statement=f"{item.kind} {item.resource_name or item.resource_id}",
           error=error or None)
    return outcome


def _identity(row: DataConnection) -> tuple[str, ...] | None:
    """Which platform resource a connection is -- however it was created."""
    settings, scope = row.settings or {}, row.scope or {}
    if row.platform == "fabric":
        if settings.get("sql_endpoint"):
            return ("fabric", "warehouse", str(settings.get("warehouse_id", "")))
        models = settings.get("semantic_models") or []
        if len(models) == 1:
            return ("fabric", "model", str(settings.get("workspace_id", "")),
                    str(models[0].get("id", "")))
    elif row.platform == "databricks":
        return ("databricks", "warehouse", str(settings.get("warehouse_id", "")),
                str(scope.get("catalog", "")).lower())
    elif row.platform == "snowflake":
        return ("snowflake", "warehouse", str(settings.get("account", "")),
                str(settings.get("warehouse", "")).lower(), str(scope.get("catalog", "")).lower())
    return None


def _free_name(taken: set[str], hint: str) -> str:
    base = " ".join(hint.split())[:90] or "Connection"
    name, number = base, 2
    while name.lower() in taken:
        name, number = f"{base} ({number})", number + 1
    return name


def _save_connection(session: Any, user: User, platform: str, spec: Spec) -> uuid.UUID:
    rows = connections.list_for_user(session, user.id)
    row = next((r for r in rows if _identity(r) == spec.identity), None)
    if row is None:
        settings = dict(spec.settings)
        if platform == "fabric" and "sql_endpoint" in settings:
            settings.setdefault("semantic_models", [])
        row = connections.create(
            session, user, platform=platform,
            name=_free_name({r.name.lower() for r in rows}, spec.name_hint),
            scope=spec.scope, settings=settings, credential_id=spec.credential_id)
        return row.id
    # Same resource set up again: new scope and address, but the person's own name,
    # notes and anything else the connection holds (e.g. its semantic models) stay.
    row.scope = dict(spec.scope)
    row.settings = {**(row.settings or {}), **spec.settings}
    row.credential_id = spec.credential_id
    row.status = "active"
    session.flush()
    return row.id


# --- reading back ----------------------------------------------------------------------------


def items_for(db: Any, user_id: uuid.UUID) -> list[SetupItem]:
    """
    The person's setup items, minus connected ones whose connection was since
    deleted on the dashboard (they no longer describe anything real).
    """
    with db.session() as session:
        items = list(session.scalars(
            select(SetupItem).where(SetupItem.user_id == user_id)
            .order_by(SetupItem.platform, SetupItem.workspace_name, SetupItem.kind,
                      SetupItem.resource_name)))
        live = set(session.scalars(select(DataConnection.id).where(
            DataConnection.user_id == user_id)))
        gone = [i for i in items if i.status == "connected"
                and (i.connection_id is None or i.connection_id not in live)]
        for item in gone:
            session.delete(item)
        kept = [i for i in items if i not in gone]
        for item in kept:
            session.expunge(item)
    return kept


def remove_item(db: Any, user_id: uuid.UUID, item_id: uuid.UUID) -> bool:
    """Forget a picked resource that is not connected. Its owner only."""
    with db.session() as session:
        removed = session.execute(
            SetupItem.__table__.delete().where(
                SetupItem.id == item_id, SetupItem.user_id == user_id,
                SetupItem.status.in_(("pending", "failed")))
        ).rowcount
    return bool(removed)


def owns_item(db: Any, user_id: uuid.UUID, item_id: uuid.UUID) -> bool:
    with db.session() as session:
        item = session.get(SetupItem, item_id)
        return item is not None and item.user_id == user_id


def mark_failed(db: Any, user_id: uuid.UUID, item_id: uuid.UUID, error: str) -> None:
    """Record why an item could not even be tried (e.g. its schemas couldn't be listed)."""
    with db.session() as session:
        session.execute(update(SetupItem).where(
            SetupItem.id == item_id, SetupItem.user_id == user_id).values(
            status="failed", error=error[:MAX_ERROR], updated_at=utcnow()))
