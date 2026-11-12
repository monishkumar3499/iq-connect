"""
Snowflake setup: the person's own Snowflake accounts (each signed in with Microsoft through
External OAuth, or with Snowflake OAuth), then each account's warehouses, databases and
schemas -- always in one role, checked rather than assumed.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

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
from backend.platforms.snowflake.auth import (
    SNOWFLAKE_OAUTH,
    SNOWFLAKE_SCOPE,
    SSO,
    SnowflakeRejected,
    account_id,
    entra_scope,
    role_name,
)
from backend.platforms.snowflake.client import secondary_roles_active
from backend.shared.models import SetupItem, User

CALLBACK = "/auth/platform/snowflake/callback"


class SnowflakeSetup(AccountPlatformSetup):
    key = "snowflake"
    label = "Snowflake"
    has_catalogs = True
    method_labels = {SSO: "Microsoft account (External OAuth)",
                     SNOWFLAKE_OAUTH: "Snowflake sign-in"}
    warehouse_note = ("Warehouses your role may use. Each one you pick becomes its own "
                      "connection, limited to the database and schemas you choose for it.")
    model_note = "Snowflake has no semantic models in IQ Connect; use warehouses."

    # --- signing in ---------------------------------------------------------------------

    def how(self, account: AccountInfo) -> tuple[str, Any, str]:
        if account.method == SNOWFLAKE_OAUTH:
            return "snowflake", self.state.snowflake_oauth, SNOWFLAKE_SCOPE
        return "entra", self.state.entra, str(account.options.get("entra_scope", ""))

    def role(self, account: AccountInfo) -> str:
        return str(account.options.get("role") or "")

    def default_account(self) -> AccountInfo | None:
        settings = self.state.settings
        if not settings.snowflake_account:
            return None
        if settings.snowflake_entra_scope and not settings.snowflake_configured:
            scope, role = entra_scope(settings.snowflake_entra_scope)
            if linked_account(self.state.db, self.user_id, "entra") is None:
                return None
            return AccountInfo(None, self.key, settings.snowflake_account, SSO,
                               {"entra_scope": scope, "role": role})
        if settings.snowflake_configured and linked_account(
                self.state.db, self.user_id, "snowflake") is not None:
            return AccountInfo(None, self.key, settings.snowflake_account, SNOWFLAKE_OAUTH,
                               {"role": settings.snowflake_role})
        return None

    def methods(self) -> list[Method]:
        settings = self.state.settings
        account = Field("account", "Snowflake account identifier", "myorg-myaccount",
                        value=settings.snowflake_account,
                        hint="The part before .snowflakecomputing.com in your Snowflake URL.")
        server_app = settings.snowflake_configured
        return [
            Method(SSO, self.method_labels[SSO],
                   "Your company Microsoft account. Your Snowflake admin sets up External OAuth "
                   "trusting your Microsoft tenant, with one Entra scope per Snowflake role.",
                   recommended=True, available=self.state.entra.ready(),
                   why_not="Microsoft sign-in isn't set up on this server yet.",
                   fields=(account, Field(
                       "entra_scope", "Entra scope for your Snowflake role",
                       "api://<resource app id>/session:scope:analyst",
                       value=settings.snowflake_entra_scope,
                       hint="From your Snowflake admin. It names the one role Claude may use."))),
            Method(SNOWFLAKE_OAUTH, self.method_labels[SNOWFLAKE_OAUTH],
                   "Snowflake's own sign-in page (your company SSO if Snowflake uses it), for a "
                   "custom OAuth security integration in your account.",
                   fields=(
                       account,
                       Field("client_id", "OAuth client ID", required=False,
                             hint="From SYSTEM$SHOW_OAUTH_CLIENT_SECRETS. Leave empty to use "
                                  "this server's integration (its own account only)."
                             if server_app else "From SYSTEM$SHOW_OAUTH_CLIENT_SECRETS."),
                       Field("client_secret", "OAuth client secret", secret=True, required=False,
                             hint="Stored encrypted, never shown again."),
                       Field("role", "Role", "ANALYST", value=settings.snowflake_role,
                             required=False,
                             hint="The one role Claude may use. Empty = your default role."),
                   ), redirect_uri=f"{settings.public_base_url}{CALLBACK}"),
        ]

    def save_sign_in(self, method: str, values: dict[str, Any], next_page: str) -> str:
        chosen = self.method(method)
        given = check_values(chosen, values)
        settings = self.state.settings
        try:
            account = account_id(given["account"])
            if chosen.key == SSO:
                scope, role = entra_scope(given["entra_scope"])
            else:
                role = role_name(given.get("role", ""))
                client_id = given.get("client_id", "")
                client_secret = given.get("client_secret", "")
                server_app = not client_id
                if server_app and not (settings.snowflake_configured
                                       and account == settings.snowflake_account):
                    raise SnowflakeRejected("Enter the OAuth client ID and secret of your "
                                            "account's security integration.")
                if not server_app and not client_secret:
                    raise SnowflakeRejected("Enter the OAuth client secret.")
        except SnowflakeRejected as error:
            raise SetupProblem(str(error)) from None
        target = self.warehouses_page(account)
        if chosen.key == SSO:
            account_ref = self.keep(address=account, method=SSO,
                                    options={"entra_scope": scope, "role": role},
                                    credential_id=linked_account(self.state.db, self.user_id,
                                                                 "entra"))
            # Always through Microsoft: the person consents to this role's scope there.
            return "/auth/platform/entra/start?" + urlencode(
                {"next": target, "platform": self.key, "account": str(account_ref)})
        secret: dict[str, Any] = {"account": account, "role": role}
        if server_app:
            secret["server_app"] = True
        else:
            secret.update(client_id=client_id, client_secret=client_secret)
        with self.state.db.session() as session:
            user = session.get(User, self.user_id)
            if user is None:
                raise SetupProblem("Sign in again.")
            credential_id = save_secret(session, self.state.secrets, user=user,
                                        provider="snowflake", account=account, secret=secret).id
        account_ref = self.keep(address=account, method=SNOWFLAKE_OAUTH, options={"role": role},
                                credential_id=credential_id)
        return "/auth/platform/snowflake/start?" + urlencode(
            {"next": target, "account": str(account_ref)})

    # --- browsing -----------------------------------------------------------------------

    def _adapter(self) -> Any:
        return self.state.pipeline.registry.get("snowflake")

    def _api(self, workspace_id: str, secret: dict[str, Any] | None = None) -> Any:
        account = self.use(workspace_id)
        return self._adapter().api(secret or self.secret_for(account), self.role(account),
                                   account.address)

    def warehouses(self, workspace_id: str) -> list[Resource]:
        return [Resource(w["name"], w["name"],
                         " · ".join(x for x in ((w.get("size") or "").title(),
                                                 (w.get("state") or "").lower()) if x),
                         {"state": w.get("state", "")})
                for w in self._api(workspace_id).warehouses()]

    def catalogs(self, workspace_id: str) -> list[str]:
        return self._api(workspace_id).databases()

    def schemas(self, workspace_id: str, warehouse: Resource, catalog: str) -> list[str]:
        return self._api(workspace_id).schemas(catalog)

    def connection_schemas(self, scope: dict[str, Any], settings: dict[str, Any]) -> list[str]:
        account = str(settings.get("account") or self.state.settings.snowflake_account)
        warehouse = Resource(str(settings.get("warehouse", "")), str(scope.get("catalog", "")))
        return self.schemas(account, warehouse, str(scope.get("catalog", "")))

    def resolve(self, item: SetupItem) -> Spec:
        account = self.use(item.workspace_id)
        secret = self.secret_for(account)
        api = self._api(item.workspace_id, secret)
        who = api.identity()
        pinned = self.role(account)
        # Least privilege, checked rather than assumed: the role must be the pinned one,
        # and no secondary roles may widen it behind the person's back.
        if pinned and who.get("role_name", "").upper() != pinned:
            raise SetupProblem(f"Snowflake opened role {who.get('role_name') or '(none)'}, "
                               f"not {pinned}. Ask your Snowflake admin to grant "
                               f"{pinned} to you.")
        if secondary_roles_active(who.get("secondary_roles", "")):
            raise SetupProblem("Your Snowflake user has secondary roles switched on, which "
                               "would give Claude more than one role. Ask your Snowflake "
                               "admin to set DEFAULT_SECONDARY_ROLES = () for your user.")
        warehouse = find(self.warehouses(item.workspace_id), item.resource_id)
        if warehouse is None:
            raise SetupProblem("That warehouse isn't available to your role any more.")
        if item.catalog not in api.databases():
            raise SetupProblem(f"The database {item.catalog} isn't visible to your role.")
        chosen = self.chosen_schemas(item, api.schemas(item.catalog))
        settings: dict[str, Any] = {"warehouse": warehouse.id,
                                    "role": pinned or who.get("role_name", ""),
                                    "account": account.address}
        if account.method == SSO:
            settings["entra_scope"] = str(account.options.get("entra_scope", ""))
        ctx = ConnectionContext(id="setup", name=warehouse.name, platform="snowflake",
                                scope={"catalog": item.catalog, "schemas": chosen},
                                settings=settings, credential_version=None)
        # Browsing the chosen schemas is the probe: SHOW needs no running warehouse, so
        # checking never resumes (and bills) a suspended one.
        self._adapter().list_objects(ctx, secret, None)
        return Spec(
            identity=("snowflake", "warehouse", account.address, warehouse.id.lower(),
                      item.catalog.lower()),
            name_hint=f"{warehouse.name} - {item.catalog}",
            scope={"catalog": item.catalog, "schemas": chosen},
            settings=settings,
            credential_id=self.credential_for(account),
        )
