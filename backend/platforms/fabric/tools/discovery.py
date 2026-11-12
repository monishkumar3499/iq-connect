"""
Fabric setup: signing in (Microsoft single sign-on, or a service principal), then the
workspaces, warehouses and semantic models that sign-in can see.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlencode

from backend.core.connection_manager.credentials import linked_account, provider_token, save_link
from backend.core.connection_manager.drivers import (
    MODEL,
    Field,
    Method,
    PlatformSetup,
    Resource,
    SetupProblem,
    Spec,
    check_values,
    find,
)
from backend.core.connection_manager.platform_accounts import (
    AccountInfo,
    find_account,
    save_account,
)
from backend.core.dynamic_registry.base import ConnectionContext
from backend.core.security.policy import check_dax
from backend.platforms.fabric.auth import (
    FABRIC_API_SCOPE,
    POWERBI_SCOPE,
    SERVICE_PRINCIPAL,
    SERVICE_PRINCIPAL_PROVIDER,
    SQL_SCOPE,
    SSO,
    ServicePrincipalRejected,
    check_service_principal_fields,
)
from backend.shared.models import SetupItem, User

METHOD_LABELS = {SSO: "Microsoft account (single sign-on)",
                 SERVICE_PRINCIPAL: "Service principal"}


def warehouse_probe(warehouse: dict[str, str]) -> ConnectionContext:
    """A connection-shaped description of a warehouse, to list its schemas."""
    return ConnectionContext(
        id="setup", name=warehouse["name"], platform="fabric",
        scope={"catalog": warehouse["name"], "schemas": []},
        settings={"sql_endpoint": warehouse["sql_endpoint"]}, credential_version=None,
    )


# A trivial, read-only DAX query: proves the sign-in may query the model.
PROBE_DAX = 'EVALUATE ROW("ok", 1)'


class FabricSetup(PlatformSetup):
    key = "fabric"
    label = "Microsoft Fabric"
    has_models = True
    sign_in_label = "Microsoft account (single sign-on)"
    warehouse_note = ("Fabric Warehouses with a SQL endpoint. Lakehouses and other item "
                      "types are not listed: IQ Connect does not support them yet.")
    model_note = ("Power BI / Fabric semantic models, queried with read-only DAX. Needs the "
                  "Power BI permission on the app registration and Build access to the model.")

    def __init__(self, state: Any, user_id: uuid.UUID) -> None:
        super().__init__(state, user_id)
        self.chosen: AccountInfo | None = find_account(state.db, user_id, "fabric", "")

    # --- which sign-in ------------------------------------------------------------------

    def _service_principal(self) -> uuid.UUID | None:
        chosen = self.chosen
        if chosen and chosen.method == SERVICE_PRINCIPAL and chosen.credential_id:
            return chosen.credential_id
        return None

    @property
    def provider(self) -> str:  # type: ignore[override]
        return SERVICE_PRINCIPAL_PROVIDER if self._service_principal() else "entra"

    def _tokens(self) -> Any:
        return self.state.fabric_sp if self._service_principal() else self.state.entra

    def credential_id(self) -> uuid.UUID | None:
        return self._service_principal() or linked_account(self.state.db, self.user_id, "entra")

    def linked(self) -> bool:
        return self.credential_id() is not None

    def account(self) -> str:
        return self.username(self._service_principal())

    def sign_in_url(self, back_to: str) -> str:
        if self._service_principal() or not self.linked():
            # A service principal's secret can only be entered again; nothing linked yet:
            # the person picks how to sign in.
            return self.sign_in_page(back_to)
        # Signed in with Microsoft, but Fabric wasn't granted (or the link expired).
        return "/auth/platform/entra/start?" + urlencode({"next": back_to, "platform": "fabric"})

    def methods(self) -> list[Method]:
        microsoft = self.state.entra.ready()
        return [
            Method(SSO, METHOD_LABELS[SSO],
                   "Your own Microsoft work account. Claude sees exactly what you can see in "
                   "Fabric, with your permissions and row-level security.",
                   recommended=True, available=microsoft,
                   why_not="Microsoft sign-in isn't set up on this server yet."),
            Method(SERVICE_PRINCIPAL, METHOD_LABELS[SERVICE_PRINCIPAL],
                   "An Entra app of your organisation (tenant ID, client ID, client secret). "
                   "Queries run as the app: add it to your Fabric workspaces, and switch on "
                   "'Service principals can use Fabric APIs' in the Fabric admin portal.",
                   fields=(
                       Field("tenant_id", "Directory (tenant) ID",
                             "11111111-2222-3333-4444-555555555555"),
                       Field("client_id", "Application (client) ID",
                             "11111111-2222-3333-4444-555555555555"),
                       Field("client_secret", "Client secret value", secret=True,
                             hint="Certificates & secrets → the secret's Value. Stored "
                                  "encrypted, never shown again."),
                   )),
        ]

    def save_sign_in(self, method: str, values: dict[str, Any], next_page: str) -> str:
        chosen = self.method(method)
        if chosen.key == SSO:
            credential_id = linked_account(self.state.db, self.user_id, "entra")
            with self.state.db.session() as session:
                save_account(session, user_id=self.user_id, platform="fabric", address="",
                             method=SSO, credential_id=credential_id)
            if credential_id is not None:
                return next_page
            return "/auth/platform/entra/start?" + urlencode({"next": next_page,
                                                               "platform": "fabric"})
        given = check_values(chosen, values)
        try:
            tenant_id, client_id, client_secret = check_service_principal_fields(
                given["tenant_id"], given["client_id"], given["client_secret"])
            self.state.fabric_sp.check(tenant_id, client_id, client_secret)
        except ServicePrincipalRejected as error:
            raise SetupProblem(str(error)) from None
        with self.state.db.session() as session:
            user = session.get(User, self.user_id)
            if user is None:
                raise SetupProblem("Sign in again.")
            credential = save_link(
                session, self.state.secrets, user=user, provider=SERVICE_PRINCIPAL_PROVIDER,
                account=f"{tenant_id}:{client_id}",
                secret={"tenant_id": tenant_id, "client_id": client_id,
                        "client_secret": client_secret,
                        "username": f"Service principal {client_id}"})
            save_account(session, user_id=self.user_id, platform="fabric", address="",
                         method=SERVICE_PRINCIPAL, credential_id=credential.id)
        return next_page

    def accounts(self) -> list[dict[str, str]]:
        if not self.linked():
            return []
        method = SERVICE_PRINCIPAL if self._service_principal() else SSO
        return [{"id": str(self.chosen.id) if self.chosen else "sso", "address": "",
                 "method": method, "methodLabel": METHOD_LABELS[method],
                 "username": self.account(), "next": self.page("/app/setup/fabric")}]

    # --- browsing -----------------------------------------------------------------------

    def _token(self, scope: str) -> str:
        state = self.state
        return provider_token(state.db, state.secrets, self._tokens(), self.provider,
                              self.user_id, scope, self._service_principal())[1]

    def _api(self) -> Any:
        return self.state.fabric_api(self._token(FABRIC_API_SCOPE))

    def _adapter(self) -> Any:
        return self.state.pipeline.registry.get("fabric")

    def workspaces(self) -> list[Resource]:
        return [Resource(w["id"], w.get("name", "")) for w in self._api().workspaces()]

    def warehouses(self, workspace_id: str) -> list[Resource]:
        return [Resource(w["id"], w.get("name", ""), "Warehouse",
                         {"sql_endpoint": w["sql_endpoint"]})
                for w in self._api().warehouses(workspace_id) if w.get("sql_endpoint")]

    def models(self, workspace_id: str) -> list[Resource]:
        lister = getattr(self._api(), "semantic_models", None)
        return [Resource(m["id"], m.get("name", ""), "Semantic model")
                for m in (lister(workspace_id) if lister else [])]

    def _probe_context(self, warehouse: Resource, schemas: list[str]) -> ConnectionContext:
        base = warehouse_probe({"name": warehouse.name,
                                "sql_endpoint": warehouse.extra["sql_endpoint"]})
        return ConnectionContext(id=base.id, name=base.name, platform="fabric",
                                 scope={"catalog": warehouse.name, "schemas": schemas},
                                 settings=base.settings, credential_version=None)

    def schemas(self, workspace_id: str, warehouse: Resource, catalog: str) -> list[str]:
        if not warehouse.extra.get("sql_endpoint"):
            raise SetupProblem("This connection has no SQL endpoint saved. Connect the "
                               "warehouse again from Add a new connection.")
        secret = {"access_tokens": {SQL_SCOPE: self._token(SQL_SCOPE)}}
        return self._adapter().list_schemas(self._probe_context(warehouse, []), secret)

    def resolve(self, item: SetupItem) -> Spec:
        workspace = self.workspace(item.workspace_id)
        credential_id = self.credential_id()
        if item.kind == MODEL:
            return self._resolve_model(item, workspace, credential_id)
        warehouse = find(self.warehouses(workspace.id), item.resource_id)
        if warehouse is None:
            raise SetupProblem("That warehouse is no longer visible to you, or has no SQL "
                               "endpoint.")
        chosen = self.chosen_schemas(item, self.schemas(workspace.id, warehouse, ""))
        secret = {"access_tokens": {SQL_SCOPE: self._token(SQL_SCOPE)}}
        self._adapter().list_objects(self._probe_context(warehouse, chosen), secret, None)
        return Spec(
            identity=("fabric", "warehouse", warehouse.id),
            name_hint=warehouse.name,
            scope={"catalog": warehouse.name, "schemas": chosen},
            settings={"workspace_id": workspace.id, "warehouse_id": warehouse.id,
                      "sql_endpoint": warehouse.extra["sql_endpoint"]},
            credential_id=credential_id,
        )

    def _resolve_model(self, item: SetupItem, workspace: Resource, credential_id) -> Spec:
        model = find(self.models(workspace.id), item.resource_id)
        if model is None:
            raise SetupProblem("That semantic model is no longer visible to you.")
        check_dax(PROBE_DAX)
        ctx = ConnectionContext(id="setup", name=model.name, platform="fabric",
                                scope={"catalog": "", "schemas": []},
                                settings={"workspace_id": workspace.id}, credential_version=None)
        self._adapter().execute_dax(
            ctx, {"access_tokens": {POWERBI_SCOPE: self._token(POWERBI_SCOPE)}},
            model.id, PROBE_DAX, 1)
        return Spec(
            identity=("fabric", "model", workspace.id, model.id),
            name_hint=f"{model.name} (semantic model)",
            scope={"catalog": "", "schemas": []},
            settings={"workspace_id": workspace.id,
                      "semantic_models": [{"id": model.id, "name": model.name}]},
            credential_id=credential_id,
        )
