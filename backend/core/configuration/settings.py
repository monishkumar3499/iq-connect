"""
Every setting the service reads, typed and validated once at startup.

Values come from environment variables, then `.env`. A bad value stops the
service at startup with a clear message instead of failing later inside a
sign-in, where Claude would only report "Authorization failed".
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    public_base_url: str = "http://localhost:8000"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    # Any SQLAlchemy URL. SQLite for now; PostgreSQL later is just a new URL.
    database_url: str = "sqlite:///.local/iq.db"
    # Where the key that protects stored secrets lives. Only "local" (a key
    # file) exists today; Azure Key Vault will be added as a second option.
    key_provider: Literal["local"] = "local"
    local_key_file: str = ".local/keys.json"
    local_signing_key_file: str = ".local/signing_keys.json"

    # OAuth server (we are the authorization server Claude signs in through).
    access_token_minutes: int = 60
    refresh_token_days: int = 30
    # Hosts a registered client may send people back to. Loopback addresses
    # (127.0.0.1, localhost) are always allowed: Claude Code uses them.
    oauth_redirect_hosts: str = "claude.ai,claude.com"
    # Client ID Metadata Documents: the only hosts we will ever fetch one from.
    oauth_cimd_enabled: bool = True
    oauth_cimd_hosts: str = "claude.ai,claude.com"

    # Signing people in to this service.
    #   entra -- "Sign in with Microsoft" (the Microsoft app: ENTRA_* or the setup UI)
    #   dev   -- type any email, no password. Refused outside APP_ENV=dev.
    sso_provider: Literal["entra", "dev"] = "dev"
    # IQ Connect serves any company: "organizations" (the default) lets every Microsoft
    # work or school account sign in, and the Microsoft app must then be multi-tenant.
    # A tenant id (GUID) instead accepts only that one company. SSO_ALLOWED_TENANTS can
    # narrow "organizations" to a list. The AZURE_* names are accepted too.
    entra_tenant: str = Field(
        "organizations",
        validation_alias=AliasChoices("entra_tenant", "entra_tenant_id", "azure_tenant_id"),
    )
    entra_client_id: str = Field(
        "", validation_alias=AliasChoices("entra_client_id", "azure_client_id"))
    entra_client_secret: SecretStr = Field(
        SecretStr(""), validation_alias=AliasChoices("entra_client_secret", "azure_client_secret"))
    sso_allowed_tenants: str = ""
    # Server operators (comma-separated emails): they become IQ admins when they sign in
    # and may change the Microsoft app entered in the setup UI. Nobody else ever does --
    # with every company allowed in, "the first person to sign in" could be anyone.
    admin_emails: str = ""
    # How long a browser stays signed in to our pages.
    session_hours: int = 8

    # Tool calls (see the capacity section of the architecture).
    platform_threads: int = 16
    calls_per_person: int = 2
    busy_wait_seconds: float = 10
    query_timeout_seconds: float = 60
    platform_login_timeout_seconds: int = 30
    # Fabric connection reuse: idle ones close after this long, and none lives
    # longer than the max age (kept below an access token's hour).
    fabric_connection_idle_seconds: float = 300
    fabric_connection_max_age_seconds: float = 2700
    default_max_rows: int = 100
    max_rows_limit: int = 2000
    max_result_chars: int = 60_000
    # Databricks defaults. Each person enters their own workspace URL in the setup; these
    # only pre-fill it (and offer this server's Databricks OAuth app, if one is set up).
    databricks_host: str = ""
    databricks_oauth_client_id: str = ""
    databricks_oauth_client_secret: SecretStr = SecretStr("")

    # Snowflake defaults. Each person enters their own Snowflake account in the setup and
    # signs in with Microsoft (External OAuth trusting Entra) or Snowflake OAuth (a custom
    # security integration); these only pre-fill those fields. README section 7.4.
    snowflake_account: str = ""
    snowflake_oauth_client_id: str = ""
    snowflake_oauth_client_secret: SecretStr = SecretStr("")
    # The one role queries may use (least privilege). Empty = each person's default role.
    snowflake_role: str = ""
    # External OAuth: the Entra scope that opens one Snowflake role,
    # e.g. api://<resource app id>/session:scope:analyst
    snowflake_entra_scope: str = ""

    # Where /setup's "Add to Claude" opens Claude's connector settings. Only a link
    # for the person's browser: nothing here is ever fetched by the server.
    claude_connectors_url: str = "https://claude.ai/settings/connectors"
    # Where "Open Claude" goes once setup is finished outside Claude's sign-in.
    claude_chat_url: str = "https://claude.ai/new"
    # The built React app (`npm run build` in frontend/). Empty = frontend/dist in the
    # project folder.
    frontend_dir: str = ""
    # Entering app registrations (Microsoft, Databricks) in the setup UI before anyone
    # is an admin: whether the operator's setup code is required. Unset = required
    # everywhere except APP_ENV=dev, where the first person to enter them may.
    setup_code_required: bool | None = None

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def issuer(self) -> str:
        """Our OAuth issuer identifier: exactly the public URL, no trailing slash."""
        return self.public_base_url

    @property
    def mcp_url(self) -> str:
        """The connector URL people paste into Claude."""
        return f"{self.public_base_url}/mcp"

    @property
    def entra_configured(self) -> bool:
        """An Entra app registration is set up: needed for Fabric (and Entra sign-in)."""
        return bool(self.entra_client_id and self.entra_client_secret.get_secret_value())

    @property
    def databricks_configured(self) -> bool:
        """This server has a default Databricks OAuth app (people may also bring their own)."""
        return bool(self.databricks_host and self.databricks_oauth_client_id)

    @field_validator("claude_connectors_url", "claude_chat_url")
    @classmethod
    def _https_link(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("https://"):
            raise ValueError("CLAUDE_CONNECTORS_URL and CLAUDE_CHAT_URL must be https:// "
                             "addresses")
        return value

    @field_validator("databricks_host")
    @classmethod
    def _workspace_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not value.startswith("https://"):
            raise ValueError("DATABRICKS_HOST must be the workspace's https:// address")
        return value

    @property
    def snowflake_configured(self) -> bool:
        return bool(self.snowflake_account and self.snowflake_oauth_client_id
                    and self.snowflake_oauth_client_secret.get_secret_value())

    @property
    def admins(self) -> frozenset[str]:
        return frozenset(e.strip().lower() for e in self.admin_emails.split(",") if e.strip())

    @field_validator("snowflake_account")
    @classmethod
    def _account_identifier(cls, value: str) -> str:
        value = value.strip().lower().removesuffix(".snowflakecomputing.com")
        value = value.removeprefix("https://")
        # Only an account identifier: it becomes part of a URL we call.
        if value and not re.fullmatch(r"[a-z0-9_.-]+", value):
            raise ValueError("SNOWFLAKE_ACCOUNT must be an account identifier like "
                             "myorg-myaccount or xy12345.eu-west-1")
        return value

    @field_validator("snowflake_role")
    @classmethod
    def _role_name(cls, value: str) -> str:
        value = value.strip()
        # Part of an OAuth scope and of every request: an identifier, nothing more.
        if value and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,254}", value):
            raise ValueError("SNOWFLAKE_ROLE must be a plain Snowflake role name, like ANALYST")
        return value.upper()

    @field_validator("snowflake_entra_scope")
    @classmethod
    def _entra_scope(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        # One scope per role pins the role; "any role" would let a token open every role
        # the person has, which is exactly the privilege creep this setting prevents.
        if "session:role-any" in value or "session:scope-any" in value:
            raise ValueError("SNOWFLAKE_ENTRA_SCOPE must name one role "
                             "(.../session:scope:<role>), not any role")
        if not re.fullmatch(r"(api://|https://)\S+/session:scope:[A-Za-z0-9_$]+", value):
            raise ValueError("SNOWFLAKE_ENTRA_SCOPE looks like "
                             "api://<resource app id>/session:scope:<role>")
        return value

    @property
    def requires_setup_code(self) -> bool:
        if self.setup_code_required is None:
            return not self.is_dev
        return self.setup_code_required

    @property
    def secure_cookies(self) -> bool:
        return self.public_base_url.startswith("https://")

    @property
    def allowed_tenants(self) -> frozenset[str] | None:
        """Entra tenants whose people may sign in; None = every company (multi-tenant)."""
        if _GUID.match(self.entra_tenant):
            return frozenset({self.entra_tenant.lower()})
        listed = frozenset(t.lower() for t in _host_set(self.sso_allowed_tenants))
        return listed or None

    @property
    def redirect_hosts(self) -> frozenset[str]:
        return _host_set(self.oauth_redirect_hosts)

    @property
    def cimd_hosts(self) -> frozenset[str]:
        return _host_set(self.oauth_cimd_hosts)

    @field_validator("entra_tenant", "entra_client_id")
    @classmethod
    def _trim(cls, value: str) -> str:
        # A stray space or quote around a pasted id must not make it unrecognisable.
        return value.strip().strip("'\"").strip()

    @field_validator("public_base_url")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        # Claude compares the issuer and resource URLs we advertise with the
        # URLs it fetched, character for character. A stray trailing slash
        # makes it abandon the sign-in without saying why.
        return value.strip().rstrip("/")

    @field_validator("log_level", mode="before")
    @classmethod
    def _lowercase(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _public_url_is_usable(self) -> Settings:
        parts = urlsplit(self.public_base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError(
                f"PUBLIC_BASE_URL must be an http(s) URL, got {self.public_base_url!r}"
            )
        if parts.query or parts.fragment:
            raise ValueError("PUBLIC_BASE_URL must not contain a query string or fragment")
        if not self.is_dev:
            # Claude reaches us from Anthropic's cloud over HTTPS. A production
            # URL that is plain http or points at this machine can never work.
            if parts.scheme != "https":
                raise ValueError("PUBLIC_BASE_URL must use https outside APP_ENV=dev")
            if parts.hostname in _LOCAL_HOSTS:
                raise ValueError("PUBLIC_BASE_URL must be a public address outside APP_ENV=dev")

        if self.sso_provider == "dev" and not self.is_dev:
            # The dev sign-in accepts any email with no password.
            raise ValueError("SSO_PROVIDER=dev is only allowed with APP_ENV=dev")
        # SSO_PROVIDER=entra without ENTRA_* values is allowed: the app registration
        # can then be entered once in the setup UI (credentials/microsoft_app.py).
        if bool(self.entra_client_id) != bool(self.entra_client_secret.get_secret_value()):
            raise ValueError("Set both ENTRA_CLIENT_ID and ENTRA_CLIENT_SECRET, or neither "
                             "(then enter them in the setup UI)")
        return self


def _host_set(value: str) -> frozenset[str]:
    return frozenset(host.strip().lower() for host in value.split(",") if host.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
