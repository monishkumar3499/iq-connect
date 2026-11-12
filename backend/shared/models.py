"""
The database schema. See README.md section 9 (Data model).

Changing anything here needs a migration:
    alembic revision --autogenerate -m "what changed"
`tests/unit/test_migrations.py` fails if the models and migrations disagree.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from backend.shared.database import JSONDict, UTCDateTime, utcnow

# Explicit constraint names. Without them each database invents its own, and a
# later migration that drops or alters a constraint cannot name it portably
# (SQLite's batch migrations need the names too).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# SQLite only auto-increments INTEGER primary keys; BIGINT elsewhere.
AutoIncrementId = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _Timestamps:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


class Org(_Timestamps, Base):
    """A company. Every user, connection and audit row belongs to exactly one."""

    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    # The SSO issuer (e.g. https://login.microsoftonline.com/<tenant>/v2.0).
    # Signing in through it is what places a person in this org.
    idp_issuer: Mapped[str] = mapped_column(String(500), unique=True)
    # Admin rules: allowed platforms and workspaces, limits.
    policy: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    users: Mapped[list[User]] = relationship(back_populates="org", passive_deletes=True)


class User(_Timestamps, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("org_id", "idp_subject"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    # The IdP's stable id for the person (Entra "oid"). Emails change; this doesn't.
    idp_subject: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The person's own standing instructions and context for Claude, written in the
    # setup's Instructions step (or on the dashboard). Sent with every tool answer.
    instructions: Mapped[str] = mapped_column(Text, default="", server_default="")

    org: Mapped[Org] = relationship(back_populates="users")
    credentials: Mapped[list[Credential]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    connections: Mapped[list[DataConnection]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )


# ---------------------------------------------------------------------------
# Platform logins and connections
# ---------------------------------------------------------------------------


class Credential(_Timestamps, Base):
    """
    One person's login to one provider (Entra, a Snowflake account, a Databricks
    workspace). Shared by every connection that uses it: one Entra login can
    serve both a Fabric and an Azure Databricks connection.

    The secret is envelope-encrypted (step 3): `ciphertext` is the secret
    encrypted with a random data key, `wrapped_key` is that data key encrypted
    by the key-encryption key named by `key_version`.
    """

    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("user_id", "provider", "account"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40))  # entra | snowflake | databricks
    account: Mapped[str] = mapped_column(String(500))  # tenant, account locator or workspace host
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Bumped whenever the secret changes. Warm database connections are cached
    # per (connection, version), so a refreshed or replaced login never reuses
    # a session opened with the old one.
    version: Mapped[int] = mapped_column(Integer, default=1)

    user: Mapped[User] = relationship(back_populates="credentials")


class PlatformAccount(_Timestamps, Base):
    """
    Where and how one person signs in to a platform, chosen in the setup's Sign in step:
    their Databricks workspace (`address` = its host), their Snowflake account (`address`
    = the account identifier) or Fabric (`address` = ""), and the method -- Microsoft
    single sign-on, a Databricks / Snowflake OAuth app, or a Fabric service principal.

    Every company brings its own workspaces and accounts, so these are per person, not
    per server. Nothing secret is kept here: client secrets and refresh tokens live
    encrypted in the credential this row points at. `options` holds non-secret choices
    (the Snowflake role, the Entra scope for Snowflake External OAuth).
    """

    __tablename__ = "platform_accounts"
    __table_args__ = (UniqueConstraint("user_id", "platform", "address"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(40))
    address: Mapped[str] = mapped_column(String(300), default="")
    method: Mapped[str] = mapped_column(String(40))
    options: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL")
    )


class AppRegistration(_Timestamps, Base):
    """
    The Microsoft (Entra) app registration this server signs in with, when it was
    entered in the UI rather than in `.env` (`.env` wins when both exist). One per
    server. The client secret is envelope-encrypted like a credential -- never
    hashed: OAuth needs the original to redeem codes and refresh tokens. Read and
    written only by `credentials/microsoft_app.py`.
    """

    __tablename__ = "app_registrations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(40), unique=True)  # entra | databricks
    # Entra: the company's tenant. Empty until the first Microsoft sign-in pins it
    # when the app was entered without one.
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    # Databricks: the workspace address (https://...).
    account: Mapped[str | None] = mapped_column(String(300))
    # Empty for Azure Databricks signed in with the Microsoft account.
    client_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[str] = mapped_column(String(200))
    # Bumped on every save, so sign-in clients built from the old values are replaced.
    version: Mapped[int] = mapped_column(Integer, default=1)
    validated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))


class DataConnection(_Timestamps, Base):
    """
    A named, scoped connection a person created ("Sales warehouse"). Claude
    names it in every tool call. Called DataConnection, not Connection, so it
    is never confused with a database connection in code.
    """

    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Unlinking a login must not delete the person's connections and prompts;
    # they become "needs sign-in" instead.
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), index=True
    )
    platform: Mapped[str] = mapped_column(String(40))  # fabric | databricks | snowflake
    name: Mapped[str] = mapped_column(String(100))
    custom_prompt: Mapped[str] = mapped_column(Text, default="")
    # What this connection may touch, e.g. {"workspace_id":..., "warehouse_id":...}
    # or {"catalog": "main", "schemas": ["sales"]}. Enforced on every call.
    scope: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    # Per-connection options: Snowflake role/warehouse, row limit, ...
    settings: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | needs_signin

    user: Mapped[User] = relationship(back_populates="connections")
    credential: Mapped[Credential | None] = relationship()


class SetupItem(_Timestamps, Base):
    """
    One resource a person picked in the onboarding wizard (a warehouse or a
    semantic model) and how saving it went. The connection it produced is the
    real thing Claude uses; this row only remembers the choice, its status and
    -- when it failed -- why, so a retry needs no re-picking and a failure
    never disturbs the resources that did connect.

    `resource_key` identifies the resource within (platform, workspace):
    "warehouse:<id>", "model:<id>", or for Databricks "warehouse:<id>:<catalog>".
    Unique per person, so picking the same resource again updates this row.
    """

    __tablename__ = "setup_items"
    __table_args__ = (UniqueConstraint("user_id", "platform", "workspace_id", "resource_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(40))
    workspace_id: Mapped[str] = mapped_column(String(200))
    workspace_name: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(20))  # warehouse | semantic_model
    resource_key: Mapped[str] = mapped_column(String(500))
    resource_id: Mapped[str] = mapped_column(String(200))
    resource_name: Mapped[str] = mapped_column(String(300), default="")
    catalog: Mapped[str] = mapped_column(String(300), default="")
    schemas: Mapped[list[str]] = mapped_column(JSON, default=list)
    # pending | connecting | connected | failed
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # A sentence the person can act on; never a token, URL or raw driver dump.
    error: Mapped[str] = mapped_column(Text, default="")
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connections.id", ondelete="SET NULL")
    )


# ---------------------------------------------------------------------------
# OAuth server state (we are Claude's authorization server; step 4)
#
# Codes and refresh tokens are stored as SHA-256 hashes, never as issued: a
# leaked database backup then contains nothing that can be replayed.
# ---------------------------------------------------------------------------


class OAuthClient(Base):
    """A registered client: Claude via DCR, or a cached CIMD document."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(500), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))  # dcr | cimd
    name: Mapped[str] = mapped_column(String(200), default="")
    redirect_uris: Mapped[list[str]] = mapped_column(JSON, default=list)
    client_metadata: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # CIMD documents are re-fetched once this passes.
    refresh_after: Mapped[datetime | None] = mapped_column(UTCDateTime)


class OAuthFlow(Base):
    """A Claude sign-in in progress: from /oauth/authorize until we issue a code."""

    __tablename__ = "oauth_flows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(500))
    redirect_uri: Mapped[str] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(Text, default="")
    resource: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class OAuthCode(Base):
    __tablename__ = "oauth_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(500))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(Text, default="")
    resource: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # Set on first use. A second use means the code leaked; the tokens it
    # produced get revoked (OAuth 2.1 section 4.1.3).
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class OAuthRefreshToken(Base):
    __tablename__ = "oauth_refresh_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(500))
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(Text, default="")
    resource: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Rotation: each use issues a new token and points the old one at it. An
    # old token presented again means it was stolen, so the whole chain dies.
    replaced_by: Mapped[str | None] = mapped_column(String(64))


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditEvent(Base):
    """
    One row per tool call. The ids are plain columns, not foreign keys: the
    record must outlive the user or connection it describes.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(AutoIncrementId, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    platform: Mapped[str | None] = mapped_column(String(40))
    tool: Mapped[str] = mapped_column(String(100))
    statement: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(20))  # ok | rejected | error
    error: Mapped[str | None] = mapped_column(Text)
