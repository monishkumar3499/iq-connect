"""Fabric: the adapter, Microsoft token exchange, account linking, and the full /mcp path."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from browser import Browser
from fakes import TENANT, FakeMsal, FakeWarehouse
from iq_connect.adapters.base import AdapterError, ConnectionContext, CredentialRejected
from iq_connect.adapters.fabric.adapter import FabricAdapter, token_struct
from iq_connect.adapters.fabric.scopes import FABRIC_API_SCOPE, SQL_SCOPE
from iq_connect.connections import service as connections
from iq_connect.credentials.providers.entra import EntraTokens
from iq_connect.credentials.service import read_secret, save_secret
from iq_connect.db.models import Base, Credential, DataConnection, User
from iq_connect.main import create_app
from iq_connect.settings import Settings
from iq_connect.setup.fabric import SetupFailed, add_fabric_connection
from mcp_client import Mcp, add_person, token_for

CTX = ConnectionContext(
    id="c1", name="Sales WH", platform="fabric",
    scope={"catalog": "SalesWH", "schemas": ["sales"]},
    settings={"sql_endpoint": "abc.datawarehouse.fabric.microsoft.com"},
    credential_version=1,
)
SECRET = {"access_tokens": {SQL_SCOPE: "sql-token"}}


# --- the adapter on its own ---------------------------------------------------


def adapter(warehouse: FakeWarehouse) -> FabricAdapter:
    return FabricAdapter(login_timeout=10, query_timeout=30, connect=warehouse)


def test_connects_read_only_with_the_persons_token():
    warehouse = FakeWarehouse()
    adapter(warehouse).run_query(CTX, SECRET, "SELECT 1", 10)
    connection_string, kwargs = warehouse.connects[0]
    assert "SERVER=abc.datawarehouse.fabric.microsoft.com;" in connection_string
    assert "DATABASE={SalesWH};" in connection_string
    assert "ApplicationIntent=ReadOnly;" in connection_string
    assert "Encrypt=yes;" in connection_string
    assert kwargs["attrs_before"] == {1256: token_struct("sql-token")}
    assert warehouse.sessions[0].timeout == 30
    assert warehouse.sessions[0].closed


def test_token_struct_is_length_prefixed_utf16():
    packed = token_struct("ab")
    assert packed == (4).to_bytes(4, "little") + "ab".encode("utf-16-le")


def test_database_name_cannot_inject_connection_settings():
    warehouse = FakeWarehouse()
    evil = ConnectionContext(**{**CTX.__dict__,
                                "scope": {"catalog": "x};SERVER=evil.example;{", "schemas": []}})
    adapter(warehouse).run_query(evil, SECRET, "SELECT 1", 1)
    connection_string = warehouse.connects[0][0]
    assert connection_string.count("SERVER=") == 2  # the real one, and one inside the braces
    assert "DATABASE={x}};SERVER=evil.example;{};" in connection_string


def test_run_query_truncates_and_closes():
    warehouse = FakeWarehouse()
    rows = adapter(warehouse).run_query(CTX, SECRET, "SELECT region, revenue FROM sales.x", 2)
    assert rows.columns == ["region", "revenue"]
    assert rows.rows == [("North", 1200), ("South", 800)]
    assert rows.truncated


def test_list_objects_and_describe_use_parameters():
    warehouse = FakeWarehouse()
    objects = adapter(warehouse).list_objects(CTX, SECRET, None)
    assert [(o.schema, o.name) for o in objects] == [("sales", "customers"), ("sales", "orders")]
    assert warehouse.statements[-1][1] == ("sales",)
    info = adapter(warehouse).describe_table(CTX, SECRET, "sales", "orders")
    assert [(c.name, c.type, c.nullable) for c in info.columns] == [
        ("order_id", "int", False), ("amount", "decimal(18,2)", True)]
    assert warehouse.statements[-1][1] == ("sales", "orders")


def test_list_schemas_hides_system_schemas():
    assert adapter(FakeWarehouse()).list_schemas(CTX, SECRET) == ["dbo", "sales"]


def test_driver_errors_become_readable():
    warehouse = FakeWarehouse()
    warehouse.error = RuntimeError("42S02", "[42S02] Invalid object name 'sales.nope'.")
    with pytest.raises(AdapterError, match="Invalid object name"):
        adapter(warehouse).run_query(CTX, SECRET, "SELECT * FROM sales.nope", 5)


def test_missing_token_or_address_is_refused():
    with pytest.raises(AdapterError, match="No Microsoft sign-in"):
        adapter(FakeWarehouse()).run_query(CTX, {}, "SELECT 1", 1)
    no_endpoint = ConnectionContext(**{**CTX.__dict__, "settings": {}})
    with pytest.raises(AdapterError, match="no warehouse address"):
        adapter(FakeWarehouse()).run_query(no_endpoint, SECRET, "SELECT 1", 1)


# --- Microsoft token exchange -------------------------------------------------


@pytest.fixture
def entra_settings(settings) -> Settings:
    values = {**settings.model_dump(), "entra_tenant": TENANT,
              "entra_client_id": "app-id", "entra_client_secret": "app-secret"}
    return Settings(_env_file=None, **values)


def test_access_tokens_are_cached_per_scope(entra_settings):
    msal = FakeMsal()
    tokens = EntraTokens(entra_settings, app=msal)
    first = tokens.access_token(credential_id="c", version=1, refresh_token="rt", scope=SQL_SCOPE)
    again = tokens.access_token(credential_id="c", version=1, refresh_token="rt", scope=SQL_SCOPE)
    other = tokens.access_token(credential_id="c", version=1, refresh_token="rt",
                                scope=FABRIC_API_SCOPE)
    assert first.token == again.token != other.token
    assert len(msal.redeemed) == 2


def test_rotated_refresh_token_is_returned(entra_settings):
    msal = FakeMsal()
    msal.rotate = True
    minted = EntraTokens(entra_settings, app=msal).access_token(
        credential_id="c", version=1, refresh_token="rt-0", scope=SQL_SCOPE)
    assert minted.new_refresh_token == "rt-1"


@pytest.mark.parametrize(("error", "kind"), [
    ("invalid_grant", CredentialRejected),
    ("interaction_required", CredentialRejected),
    ("temporarily_unavailable", AdapterError),
])
def test_token_errors(entra_settings, error, kind):
    msal = FakeMsal()
    msal.fail_with = error
    with pytest.raises(kind) as raised:
        EntraTokens(entra_settings, app=msal).access_token(
            credential_id="c", version=1, refresh_token="rt", scope=SQL_SCOPE)
    if kind is AdapterError:
        assert not isinstance(raised.value, CredentialRejected)


# --- a whole app with Fabric ----------------------------------------------------


@pytest.fixture
def fabric(entra_settings):
    app = create_app(entra_settings)
    Base.metadata.create_all(app.state.db.engine)
    msal, warehouse = FakeMsal(), FakeWarehouse()
    app.state.entra._app = msal
    app.state.pipeline.registry.get("fabric")._connect = warehouse
    return app, msal, warehouse


def link_and_connect(app, user_id, refresh_token: str = "rt-0") -> None:
    with app.state.db.session() as session:
        user = session.get(User, user_id)
        credential = save_secret(session, app.state.secrets, user=user, provider="entra",
                                 account=TENANT, secret={"refresh_token": refresh_token})
        connections.create(session, user, platform="fabric", name="Sales WH",
                           scope=CTX.scope, settings=CTX.settings,
                           credential_id=credential.id, custom_prompt="Revenue is in EUR.")


def test_query_runs_as_the_person_through_mcp(fabric):
    app, msal, warehouse = fabric
    alice = add_person(app, "alice@acme.com")
    link_and_connect(app, alice)
    with Mcp(app, token_for(app, alice)) as mcp:
        text, error = mcp.call("run_query", connection="Sales WH",
                               sql="SELECT region, SUM(amount) AS revenue FROM sales.orders "
                                   "GROUP BY region")
    assert not error, text
    assert "Revenue is in EUR." in text and "| North | 1200 |" in text
    assert msal.redeemed == [("rt-0", SQL_SCOPE)]
    assert warehouse.connects[0][1]["attrs_before"][1256] == token_struct(f"at-{SQL_SCOPE}-1")


def test_rejected_query_never_contacts_microsoft(fabric):
    app, msal, warehouse = fabric
    alice = add_person(app, "alice@acme.com")
    link_and_connect(app, alice)
    with Mcp(app, token_for(app, alice)) as mcp:
        text, error = mcp.call("run_query", connection="Sales WH", sql="DELETE FROM sales.orders")
    assert error and "Query rejected" in text
    assert msal.redeemed == [] and warehouse.connects == []


def test_rotated_refresh_token_is_stored_without_a_new_version(fabric):
    app, msal, _ = fabric
    msal.rotate = True
    alice = add_person(app, "alice@acme.com")
    link_and_connect(app, alice)
    with Mcp(app, token_for(app, alice)) as mcp:
        mcp.call("list_objects", connection="Sales WH")
    with app.state.db.session() as session:
        credential = session.scalars(select(Credential)).one()
        assert read_secret(app.state.secrets, credential)["refresh_token"] == "rt-1"
        assert credential.version == 1


def test_dead_microsoft_link_marks_the_connection(fabric):
    app, msal, _ = fabric
    msal.fail_with = "invalid_grant"
    alice = add_person(app, "alice@acme.com")
    link_and_connect(app, alice)
    with Mcp(app, token_for(app, alice)) as mcp:
        first, error = mcp.call("run_query", connection="Sales WH", sql="SELECT 1")
        assert error and "sign in again" in first
        second, error = mcp.call("run_query", connection="Sales WH", sql="SELECT 1")
    assert error and "needs you to sign in" in second
    assert len(msal.redeemed) == 1  # the second call didn't even try
    with app.state.db.session() as session:
        assert session.scalars(select(DataConnection)).one().status == "needs_signin"


# --- linking a Microsoft account in the browser ----------------------------------


def test_linking_stores_the_refresh_token_encrypted(fabric):
    app, _, _ = fabric
    browser = Browser(app)
    browser.sign_in("alice@acme.com")
    start = browser.http.get("/auth/platform/entra/start", params={"next": "/"})
    assert start.headers["location"].startswith("https://login.microsoftonline.com/")
    done = browser.http.get("/auth/platform/entra/callback", params={"code": "c", "state": "s"})
    assert done.status_code == 303
    with app.state.db.session() as session:
        credential = session.scalars(select(Credential)).one()
        assert credential.provider == "entra" and credential.account == TENANT
        assert b"rt-0" not in credential.ciphertext
        assert read_secret(app.state.secrets, credential)["refresh_token"] == "rt-0"
    assert "alice@acme.com" in browser.http.get("/").text  # which account is linked


def test_linking_needs_a_signed_in_person(fabric):
    app, _, _ = fabric
    response = TestClient(app, follow_redirects=False).get("/auth/platform/entra/start")
    assert response.headers["location"].startswith("/auth/login?")


def test_linking_another_companys_account_is_refused(fabric):
    app, msal, _ = fabric
    msal.link_claims["tid"] = "99999999-0000-0000-0000-000000000000"
    browser = Browser(app)
    browser.sign_in()
    browser.http.get("/auth/platform/entra/start")
    response = browser.http.get("/auth/platform/entra/callback", params={"code": "c", "state": "s"})
    assert response.status_code == 403


def test_callback_without_starting_is_refused(fabric):
    app, _, _ = fabric
    browser = Browser(app)
    browser.sign_in()
    response = browser.http.get("/auth/platform/entra/callback", params={"code": "c", "state": "s"})
    assert response.status_code == 400


def test_linking_again_repairs_waiting_connections(fabric):
    app, _, _ = fabric
    browser = Browser(app)
    browser.sign_in("alice@acme.com")
    browser.http.get("/auth/platform/entra/start")
    browser.http.get("/auth/platform/entra/callback", params={"code": "c", "state": "s"})
    with app.state.db.session() as session:
        user = session.scalars(select(User)).one()
        credential = session.scalars(select(Credential)).one()
        row = connections.create(session, user, platform="fabric", name="WH", scope=CTX.scope,
                                 settings=CTX.settings, credential_id=credential.id)
        row.status = "needs_signin"
    browser.http.get("/auth/platform/entra/start")
    browser.http.get("/auth/platform/entra/callback", params={"code": "c", "state": "s"})
    with app.state.db.session() as session:
        assert session.scalars(select(DataConnection)).one().status == "active"


# --- the setup command --------------------------------------------------------------


class FakeApi:
    def __init__(self, token: str) -> None:
        self.token = token

    def workspaces(self):
        return [{"id": "w1", "name": "Finance"}, {"id": "w2", "name": "Sales"}]

    def warehouses(self, workspace_id):
        assert workspace_id == "w2"
        return [{"id": "h1", "name": "SalesWH", "sql_endpoint": "abc.fabric.microsoft.com"}]


def scripted(*answers):
    replies = iter(answers)
    return lambda prompt: next(replies)


def test_setup_command_creates_a_scoped_connection(fabric):
    app, _, warehouse = fabric
    alice = add_person(app, "alice@acme.com")
    link_and_connect(app, alice)  # also creates "Sales WH"; the new one needs another name
    name = add_fabric_connection(
        email="ALICE@acme.com", db=app.state.db, box=app.state.secrets, entra=app.state.entra,
        public_url="http://localhost:8000",
        ask=scripted("2", "1", "2", "Sales WH", "Sales mart", "EUR amounts"),
        say=lambda text: None, api_factory=FakeApi,
        adapter=FabricAdapter(login_timeout=5, query_timeout=5, connect=warehouse),
    )
    assert name == "Sales mart"
    with app.state.db.session() as session:
        row = session.scalars(select(DataConnection).where(DataConnection.name == name)).one()
        assert row.scope == {"catalog": "SalesWH", "schemas": ["sales"]}
        assert row.settings["sql_endpoint"] == "abc.fabric.microsoft.com"
        assert row.custom_prompt == "EUR amounts"
        assert row.credential_id is not None


def test_setup_command_needs_a_linked_account(fabric):
    app, _, _ = fabric
    add_person(app, "alice@acme.com")
    with pytest.raises(SetupFailed, match="No Microsoft account is linked"):
        add_fabric_connection(email="alice@acme.com", db=app.state.db, box=app.state.secrets,
                              entra=app.state.entra, public_url="http://localhost:8000",
                              ask=scripted(), say=lambda text: None, api_factory=FakeApi)
