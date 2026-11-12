"""
Databricks setup: the person's own workspaces (each signed in with Microsoft on Azure, or
with Databricks OAuth), then each workspace's SQL warehouses, catalogs and schemas.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode, urlsplit

from backend.core.connection_manager.credentials import linked_account, save_secret
from backend.core.connection_manager.drivers import (
    AccountPlatformSetup,
    Field,
    Method,
    Resource,
    SetupProblem,
    Spec,
    check_values,
    find,
)
from backend.core.connection_manager.platform_accounts import AccountInfo
from backend.core.dynamic_registry.base import ConnectionContext
from backend.core.security.policy import check_query
from backend.platforms.databricks.auth import (
    AZURE_DATABRICKS_SCOPE,
    DATABRICKS_OAUTH,
    SSO,
    TOKEN_SCOPE,
    DatabricksRejected,
    check_workspace,
    workspace_host,
)
from backend.shared.models import SetupItem, User

CALLBACK = "/auth/platform/databricks/callback"


def _hostname(host: str) -> str:
    return urlsplit(host).hostname or ""


class DatabricksSetup(AccountPlatformSetup):
    key = "databricks"
    label = "Databricks"
    has_catalogs = True
    method_labels = {SSO: "Microsoft account (Azure Databricks)",
                     DATABRICKS_OAUTH: "Databricks sign-in"}
    warehouse_note = ("SQL warehouses you may use. Each one you pick becomes its own "
                      "connection, limited to the catalog and schemas you choose for it.")
    model_note = "Databricks has no semantic models in IQ Connect; use SQL warehouses."

    # --- signing in ---------------------------------------------------------------------

    def how(self, account: AccountInfo) -> tuple[str, Any, str]:
        if account.method == DATABRICKS_OAUTH:
            return "databricks", self.state.databricks_oauth, TOKEN_SCOPE
        return "entra", self.state.entra, AZURE_DATABRICKS_SCOPE

    def default_account(self) -> AccountInfo | None:
        settings = self.state.settings
        if not settings.databricks_host:
            return None
        method = DATABRICKS_OAUTH if settings.databricks_oauth_client_id else SSO
        provider = "databricks" if method == DATABRICKS_OAUTH else "entra"
        if method == SSO and not settings.databricks_host.endswith(".azuredatabricks.net"):
            return None
        if linked_account(self.state.db, self.user_id, provider) is None:
            return None
        return AccountInfo(None, self.key, _hostname(settings.databricks_host), method)

    def methods(self) -> list[Method]:
        settings = self.state.settings
        default_host = settings.databricks_host
        server_app = bool(settings.databricks_oauth_client_id)
        redirect = f"{settings.public_base_url}{CALLBACK}"
        host = Field("host", "Workspace URL", "https://adb-1234567890.12.azuredatabricks.net",
                     value=default_host)
        return [
            Method(SSO, self.method_labels[SSO],
                   "Your company Microsoft account, for Azure Databricks. Claude sees exactly "
                   "what you can see, with your Unity Catalog permissions.",
                   recommended=True, available=self.state.entra.ready(),
                   why_not="Microsoft sign-in isn't set up on this server yet.",
                   fields=(host,)),
            Method(DATABRICKS_OAUTH, self.method_labels[DATABRICKS_OAUTH],
                   "Databricks' own sign-in, on any cloud. Needs an OAuth App connection in "
                   "your Databricks account console (scopes all-apis, offline_access).",
                   fields=(
                       Field("host", host.label, "https://dbc-1234abcd-5678.cloud.databricks.com",
                             value=default_host),
                       Field("client_id", "OAuth client ID", required=not server_app,
                             hint="Leave empty to use this server's Databricks app."
                             if server_app else "From the App connection in the account console."),
                       Field("client_secret", "OAuth client secret", secret=True, required=False,
                             hint="Only for a confidential app. Stored encrypted."),
                   ), redirect_uri=redirect),
        ]

    def save_sign_in(self, method: str, values: dict[str, Any], next_page: str) -> str:
        chosen = self.method(method)
        given = check_values(chosen, values)
        http = self.state.databricks_oauth.http
        settings = self.state.settings
        try:
            host = workspace_host(given["host"], azure_only=chosen.key == SSO)
            if chosen.key == SSO:
                check_workspace(http, host)
            else:
                server_app = not given["client_id"]
                client_id, client_secret = ((settings.databricks_oauth_client_id,
                                             settings.databricks_oauth_client_secret
                                             .get_secret_value()) if server_app
                                            else (given["client_id"], given["client_secret"]))
                if not client_id:
                    raise DatabricksRejected("Enter the OAuth client ID.")
                check_workspace(http, host, client_id, client_secret,
                                f"{settings.public_base_url}{CALLBACK}")
        except DatabricksRejected as error:
            raise SetupProblem(str(error)) from None
        address = _hostname(host)
        target = self.warehouses_page(address)
        if chosen.key == SSO:
            credential_id = linked_account(self.state.db, self.user_id, "entra")
            account_id = self.keep(address=address, method=SSO, credential_id=credential_id)
            if credential_id is not None:
                return target
            return "/auth/platform/entra/start?" + urlencode(
                {"next": target, "platform": self.key, "account": str(account_id)})
        secret: dict[str, Any] = {"host": host}
        if server_app:
            secret["server_app"] = True
        else:
            secret.update(client_id=client_id, client_secret=client_secret)
        with self.state.db.session() as session:
            user = session.get(User, self.user_id)
            if user is None:
                raise SetupProblem("Sign in again.")
            credential = save_secret(session, self.state.secrets, user=user,
                                     provider="databricks", account=host, secret=secret)
            credential_id = credential.id
        account_id = self.keep(address=address, method=DATABRICKS_OAUTH,
                               credential_id=credential_id)
        return "/auth/platform/databricks/start?" + urlencode(
            {"next": target, "account": str(account_id)})

    # --- browsing -----------------------------------------------------------------------

    def _adapter(self) -> Any:
        return self.state.pipeline.registry.get("databricks")

    def _api(self, workspace_id: str, secret: dict[str, Any] | None = None) -> Any:
        account = self.use(workspace_id)
        return self._adapter().api(secret or self.secret_for(account), f"https://{account.address}")

    def warehouses(self, workspace_id: str) -> list[Resource]:
        return [Resource(w["id"], w.get("name", ""),
                         f"SQL warehouse, {(w.get('state') or 'unknown').lower()}",
                         {"state": w.get("state", "")})
                for w in self._api(workspace_id).warehouses()]

    def catalogs(self, workspace_id: str) -> list[str]:
        return self._api(workspace_id).catalogs()

    def schemas(self, workspace_id: str, warehouse: Resource, catalog: str) -> list[str]:
        return self._api(workspace_id).schemas(catalog)

    def connection_schemas(self, scope: dict[str, Any], settings: dict[str, Any]) -> list[str]:
        host = str(settings.get("host") or self.state.settings.databricks_host)
        warehouse = Resource(str(settings.get("warehouse_id", "")), str(scope.get("catalog", "")))
        return self.schemas(_hostname(host), warehouse, str(scope.get("catalog", "")))

    def resolve(self, item: SetupItem) -> Spec:
        account = self.use(item.workspace_id)
        secret = self.secret_for(account)
        host = f"https://{account.address}"
        api = self._api(item.workspace_id, secret)
        warehouse = find(self.warehouses(item.workspace_id), item.resource_id)
        if warehouse is None:
            raise SetupProblem("That SQL warehouse isn't available to you any more.")
        if item.catalog not in api.catalogs():
            raise SetupProblem(f"The catalog {item.catalog} isn't visible to you.")
        chosen = self.chosen_schemas(item, api.schemas(item.catalog))
        settings = {"warehouse_id": warehouse.id, "host": host}
        ctx = ConnectionContext(id="setup", name=warehouse.name, platform="databricks",
                                scope={"catalog": item.catalog, "schemas": chosen},
                                settings=settings, credential_version=None)
        adapter = self._adapter()
        adapter.list_objects(ctx, secret, None)
        if str(warehouse.extra.get("state", "")).upper() == "RUNNING":
            # Only a running warehouse is asked to run SQL: starting a stopped one
            # just to test it would cost the person money.
            check_query("SELECT 1", adapter.dialect, ctx)
            adapter.run_query(ctx, secret, "SELECT 1", 1)
        return Spec(
            identity=("databricks", "warehouse", warehouse.id, item.catalog.lower()),
            name_hint=f"{warehouse.name} - {item.catalog}",
            scope={"catalog": item.catalog, "schemas": chosen},
            settings=settings,
            credential_id=self.credential_for(account),
        )
