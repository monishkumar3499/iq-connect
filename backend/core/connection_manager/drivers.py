"""
The setup drivers' shared base: what every platform's guided setup must answer (how to
sign in, its workspaces, warehouses, schemas, and how a picked resource is re-checked and
probed), plus the list of platforms this server offers.

Each platform implements its driver in `platforms/<name>/tools/discovery.py`; the dynamic
registry (`core/dynamic_registry/registry.py`) knows which ones exist.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from sqlalchemy import select

from backend.core.connection_manager.credentials import (
    NotLinked,
    linked_account,
    provider_token,
    read_secret,
)
from backend.core.connection_manager.platform_accounts import (
    AccountInfo,
    list_accounts,
    save_account,
)
from backend.core.security.crypto import DecryptionError
from backend.shared.models import Credential, SetupItem

WAREHOUSE = "warehouse"
MODEL = "semantic_model"


class SetupProblem(Exception):
    """Something the person can fix: shown on the item, never a stack trace."""


@dataclass(frozen=True)
class Resource:
    id: str
    name: str
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Spec:
    """A validated resource, ready to become (or update) a connection."""

    identity: tuple[str, ...]
    name_hint: str
    scope: dict[str, Any]
    settings: dict[str, Any]
    credential_id: uuid.UUID | None


@dataclass(frozen=True)
class Field:
    """One value a sign-in method asks the person for."""

    name: str
    label: str
    placeholder: str = ""
    value: str = ""        # pre-filled (the server's default); never a secret
    secret: bool = False
    required: bool = True
    hint: str = ""


@dataclass(frozen=True)
class Method:
    """One way of signing in to a platform, as the Sign in step shows it."""

    key: str
    label: str
    description: str
    recommended: bool = False
    available: bool = True
    why_not: str = ""
    fields: tuple[Field, ...] = ()
    # An OAuth app the person registers needs our callback address on it.
    redirect_uri: str = ""


def find(items: list[Resource], item_id: str) -> Resource | None:
    return next((item for item in items if item.id == item_id), None)


def check_values(method: Method, values: dict[str, Any]) -> dict[str, str]:
    """The method's fields from the browser: strings, trimmed, required ones present."""
    found: dict[str, str] = {}
    for spec in method.fields:
        value = values.get(spec.name, "")
        value = value.strip() if isinstance(value, str) else ""
        if len(value) > 600:
            raise SetupProblem(f"{spec.label} is too long.")
        if spec.required and not value:
            raise SetupProblem(f"Enter {spec.label.lower()}.")
        found[spec.name] = value
    return found


class PlatformSetup:
    """One platform's setup driver, for one person. `state` is the app's state."""

    key = ""
    label = ""
    has_models = False
    has_catalogs = False
    # Which credential provider signs the person in; None = nothing to sign in to.
    provider: str | None = None
    sign_in_label = ""
    warehouse_note = ""
    model_note = ""

    def __init__(self, state: Any, user_id: uuid.UUID) -> None:
        self.state = state
        self.user_id = user_id
        # Claude's sign-in id, when the setup runs inside it: kept on every page we send to.
        self.flow = ""

    # --- signing in -------------------------------------------------------------------

    def methods(self) -> list[Method]:
        """The ways to sign in; the first available one is the default."""
        return []

    def method(self, key: str) -> Method:
        found = next((m for m in self.methods() if m.key == key and m.available), None)
        if found is None:
            raise SetupProblem("Choose one of the sign-in options.")
        return found

    def save_sign_in(self, method: str, values: dict[str, Any], next_page: str) -> str:
        """Check and keep the person's choice; where the browser goes next."""
        raise SetupProblem(f"{self.label} has nothing to sign in to.")

    def accounts(self) -> list[dict[str, str]]:
        """The person's existing sign-ins for this platform, for the Sign in step."""
        return []

    def page(self, path: str) -> str:
        """One of our setup pages, with Claude's sign-in id kept."""
        return path + ("?" + urlencode({"flow": self.flow}) if self.flow else "")

    def sign_in_page(self, back_to: str) -> str:
        """
        Our own Sign in step for this platform. Claude's sign-in id and the step to come
        back to (`back_to`, one of this platform's own setup pages) travel with it.
        """
        query = parse_qs(urlsplit(back_to).query)
        flow = self.flow or (query.get("flow") or [""])[0]
        params = {"flow": flow} if flow else {}
        if back_to.startswith(f"/app/setup/{self.key}") and "then=" in back_to:
            params["next"] = back_to
        path = f"/app/setup/{self.key}/sign-in"
        return path + ("?" + urlencode(params) if params else "")

    def linked(self) -> bool:
        return self.provider is None or linked_account(
            self.state.db, self.user_id, self.provider) is not None

    def username(self, credential_id: uuid.UUID | None = None) -> str:
        """The signed-in account's name (an email or app), for "which account is this?"."""
        if self.provider is None and credential_id is None:
            return ""
        with self.state.db.session() as session:
            query = select(Credential).where(Credential.user_id == self.user_id)
            query = query.where(Credential.id == credential_id) if credential_id else \
                query.where(Credential.provider == self.provider)
            credential = session.scalars(query).first()
            if credential is None:
                return ""
            try:
                return str(read_secret(self.state.secrets, credential).get("username") or "")
            except DecryptionError:
                return ""

    def account(self) -> str:
        return self.username()

    def sign_in_url(self, back_to: str) -> str:
        return self.sign_in_page(back_to)

    # --- browsing ---------------------------------------------------------------------

    def workspaces(self) -> list[Resource]:
        raise NotImplementedError

    def warehouses(self, workspace_id: str) -> list[Resource]:
        raise NotImplementedError

    def models(self, workspace_id: str) -> list[Resource]:
        return []

    def catalogs(self, workspace_id: str) -> list[str]:
        return []

    def schemas(self, workspace_id: str, warehouse: Resource, catalog: str) -> list[str]:
        raise NotImplementedError

    def resolve(self, item: SetupItem) -> Spec:
        """Re-check the picked resource as the person, probe it, describe its connection."""
        raise NotImplementedError

    def connection_schemas(self, scope: dict[str, Any], settings: dict[str, Any]) -> list[str]:
        """
        Every schema a saved warehouse connection could be limited to, listed as the
        person -- however the connection was made (guided setup, older versions).
        """
        catalog = str(scope.get("catalog", ""))
        warehouse = Resource(str(settings.get("warehouse_id") or settings.get("warehouse") or ""),
                             catalog, extra={"sql_endpoint": str(settings.get("sql_endpoint", ""))})
        return self.schemas(str(settings.get("workspace_id", "")), warehouse, catalog)

    def workspace(self, workspace_id: str) -> Resource:
        return self.locate(workspace_id)[0]

    def locate(self, workspace_id: str) -> tuple[Resource, int]:
        """The workspace, and how many the person can see (with one, the app skips the picker)."""
        every = self.workspaces()
        found = find(every, workspace_id)
        if found is None:
            raise SetupProblem("That workspace isn't visible to your account.")
        return found, len(every)

    def chosen_schemas(self, item: SetupItem, real: list[str]) -> list[str]:
        chosen = [str(s) for s in item.schemas or []]
        if not chosen:
            raise SetupProblem("No schemas were chosen. Use Change selection to pick at least "
                               "one schema Claude may read.")
        missing = sorted(set(chosen) - set(real))
        if missing:
            raise SetupProblem("You can no longer read these schemas: " + ", ".join(missing))
        return chosen


class AccountPlatformSetup(PlatformSetup):
    """
    A platform where each person adds their own workspaces or accounts by address
    (Databricks workspaces, Snowflake accounts). The address is the setup's workspace id;
    each one remembers how it signs in (`PlatformAccount`).
    """

    provider = None
    method_labels: dict[str, str] = {}

    def __init__(self, state: Any, user_id: uuid.UUID) -> None:
        super().__init__(state, user_id)
        # The account the current request is about: platform sign-ins go back to it.
        self.current: AccountInfo | None = None

    # --- what each platform says ----------------------------------------------------------

    def how(self, account: AccountInfo) -> tuple[str, Any, str]:
        """(credential provider, token provider, scope) for this account's method."""
        raise NotImplementedError

    def default_account(self) -> AccountInfo | None:
        """The server's default workspace, when the person already has a sign-in for it."""
        return None

    # --- shared -----------------------------------------------------------------------

    def all_accounts(self) -> list[AccountInfo]:
        found = list_accounts(self.state.db, self.user_id, self.key)
        default = self.default_account()
        if default is not None and all(a.address != default.address for a in found):
            found.append(default)
        return found

    def use(self, workspace_id: str) -> AccountInfo:
        account = next((a for a in self.all_accounts() if a.address == workspace_id), None)
        if account is None:
            raise NotLinked()
        self.current = account
        return account

    def credential_for(self, account: AccountInfo) -> uuid.UUID | None:
        provider = self.how(account)[0]
        if provider == "entra":
            return linked_account(self.state.db, self.user_id, "entra")
        return account.credential_id or linked_account(self.state.db, self.user_id, provider)

    def secret_for(self, account: AccountInfo) -> dict[str, Any]:
        provider, tokens, scope = self.how(account)
        credential_id = self.credential_for(account)
        if credential_id is None:
            raise NotLinked()
        token = provider_token(self.state.db, self.state.secrets, tokens, provider,
                               self.user_id, scope, credential_id)[1]
        return {"access_tokens": {scope: token}}

    def linked(self) -> bool:
        return bool(self.all_accounts())

    def account(self) -> str:
        return self.username(self.credential_for(self.current)) if self.current else ""

    def workspaces(self) -> list[Resource]:
        return [Resource(a.address, a.address, self.method_labels.get(a.method, a.method))
                for a in self.all_accounts()]

    def accounts(self) -> list[dict[str, str]]:
        return [{"id": str(a.id or a.address), "address": a.address, "method": a.method,
                 "methodLabel": self.method_labels.get(a.method, a.method),
                 "username": self.username(self.credential_for(a)) if self.credential_for(a)
                 else "", "next": self.warehouses_page(a.address)}
                for a in self.all_accounts()]

    def sign_in_url(self, back_to: str) -> str:
        account = self.current
        if account is None:
            return self.sign_in_page(back_to)
        provider = self.how(account)[0]
        query = {"next": back_to, "platform": self.key, "account": str(account.id or "")}
        if provider == "entra":
            return "/auth/platform/entra/start?" + urlencode(query)
        if account.id is None:
            return self.sign_in_page(back_to)
        return f"/auth/platform/{self.key}/start?" + urlencode(query)

    def keep(self, *, address: str, method: str, options: dict[str, Any] | None = None,
             credential_id: uuid.UUID | None = None) -> uuid.UUID:
        with self.state.db.session() as session:
            return save_account(session, user_id=self.user_id, platform=self.key,
                                address=address, method=method, options=options,
                                credential_id=credential_id)

    def warehouses_page(self, address: str) -> str:
        return self.page(f"/app/setup/{self.key}/ws/{address}/warehouses")


MICROSOFT_SETUP = "/app/setup/microsoft"


@dataclass(frozen=True)
class PlatformStatus:
    key: str
    label: str
    available: bool
    how: str
    why_not: str = ""
    # Where to set up what's missing (an app registration), when the setup UI can.
    setup_url: str = ""

    @property
    def needs_microsoft(self) -> bool:
        return self.setup_url.endswith("/microsoft")


def platform_statuses(state: Any) -> list[PlatformStatus]:
    """Every platform this server offers -- for /setup and step 1."""
    return [plugin.status(state) for plugin in state.platforms.plugins()]


def platform_for(state: Any, key: str, user_id: uuid.UUID) -> PlatformSetup | None:
    """The setup driver for a platform this server can set up, else None."""
    plugin = state.platforms.plugin(key)
    if plugin is None or not plugin.status(state).available:
        return None
    return plugin.setup(state, user_id)
