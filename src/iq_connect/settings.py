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
    #   entra -- "Sign in with Microsoft" (needs the three ENTRA_* values)
    #   dev   -- type any email, no password. Refused outside APP_ENV=dev.
    sso_provider: Literal["entra", "dev"] = "dev"
    # A tenant id (GUID) to accept only that company, or "organizations" for
    # any work account -- then SSO_ALLOWED_TENANTS must list who may sign in.
    # The AZURE_* names are accepted too: they are what Azure tooling (and the
    # earlier prototype) call the same three values.
    entra_tenant: str = Field(
        "organizations",
        validation_alias=AliasChoices("entra_tenant", "entra_tenant_id", "azure_tenant_id"),
    )
    entra_client_id: str = Field(
        "", validation_alias=AliasChoices("entra_client_id", "azure_client_id"))
    entra_client_secret: SecretStr = Field(
        SecretStr(""), validation_alias=AliasChoices("entra_client_secret", "azure_client_secret"))
    sso_allowed_tenants: str = ""
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
    # Databricks: one workspace per server for now, signed in with Databricks OAuth.
    databricks_host: str = ""
    databricks_oauth_client_id: str = ""
    databricks_oauth_client_secret: SecretStr = SecretStr("")

    # Snowflake: one account per server, signed in with Snowflake OAuth (a custom
    # OAuth security integration created by a Snowflake admin).
    snowflake_account: str = ""
    snowflake_oauth_client_id: str = ""
    snowflake_oauth_client_secret: SecretStr = SecretStr("")

    # The built-in sample database. On by default in dev only.
    demo_platform: bool | None = None

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
        """Databricks' own OAuth sign-in is set up (any cloud)."""
        return bool(self.databricks_host and self.databricks_oauth_client_id)

    @property
    def databricks_enabled(self) -> bool:
        """Databricks can be used: its own sign-in, or (Azure) the Microsoft account."""
        return bool(self.databricks_host
                    and (self.databricks_oauth_client_id or self.entra_configured))

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
    def snowflake_url(self) -> str:
        return f"https://{self.snowflake_account}.snowflakecomputing.com"

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

    @property
    def demo_enabled(self) -> bool:
        return self.is_dev if self.demo_platform is None else self.demo_platform

    @property
    def secure_cookies(self) -> bool:
        return self.public_base_url.startswith("https://")

    @property
    def allowed_tenants(self) -> frozenset[str]:
        """Entra tenants whose people may sign in."""
        if _GUID.match(self.entra_tenant):
            return frozenset({self.entra_tenant.lower()})
        return frozenset(t.lower() for t in _host_set(self.sso_allowed_tenants))

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
        if self.sso_provider == "entra" and not self.entra_configured:
            raise ValueError("SSO_PROVIDER=entra needs ENTRA_CLIENT_ID and ENTRA_CLIENT_SECRET")
        if self.entra_configured:
            # Also when only Fabric uses the app registration: linking a
            # Microsoft account must be limited to known tenants too.
            if not self.allowed_tenants:
                # "organizations" alone would let every Microsoft work account
                # in the world sign in.
                raise ValueError(
                    "Set ENTRA_TENANT (or ENTRA_TENANT_ID / AZURE_TENANT_ID) to your tenant "
                    "id -- a GUID like 11111111-2222-3333-4444-555555555555 -- or list "
                    "SSO_ALLOWED_TENANTS. "
                    f"The tenant value found was {self.entra_tenant!r}."
                )
        return self


def _host_set(value: str) -> frozenset[str]:
    return frozenset(host.strip().lower() for host in value.split(",") if host.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
