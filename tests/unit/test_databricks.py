"""Databricks: OAuth sign-in, the adapter, the wizard and the full /mcp path."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from browser import Browser
from fake_databricks import HOST, FakeDatabricks
from iq_connect.adapters.base import AdapterError, ConnectionContext, CredentialRejected
from iq_connect.adapters.databricks.adapter import DatabricksAdapter
from iq_connect.connections import service as connections
from iq_connect.credentials.service import read_secret, save_secret
from iq_connect.db.models import Base, Credential, DataConnection, User
from iq_connect.identity.providers import SignInFailed
from iq_connect.main import create_app
from iq_connect.settings import Settings
from mcp_client import Mcp, add_person, token_for

CTX = ConnectionContext(id="c1", name="Lakehouse", platform="databricks",
                        scope={"catalog": "main", "schemas": ["sales"]},
                        settings={"warehouse_id": "wh1"}, credential_version=1)


@pytest.fixture
def fake() -> FakeDatabricks:
    return FakeDatabricks()


@pytest.fixture
def dbx_app(settings, fake):
    values = {**settings.model_dump(), "databricks_host": HOST + "/",
              "databricks_oauth_client_id": "iq-app"}
    app = create_app(Settings(_env_file=None, **values))
    Base.metadata.create_all(app.state.db.engine)
    app.state.databricks_oauth._http = fake.client()
    app.state.pipeline.registry.get("databricks")._http = fake.client()
    return app


def adapter(fake: FakeDatabricks) -> DatabricksAdapter:
    return DatabricksAdapter(host=HOST, query_timeout=60, http=fake.client)


def signed(fake: FakeDatabricks) -> dict:
    token = fake._issue()
    return {"access_tokens": {"all-apis": token}}


# --- the adapter --------------------------------------------------------------------


def test_run_query_sends_scope_and_limits(fake):
    rows = adapter(fake).run_query(CTX, signed(fake), "SELECT region, revenue FROM x", 2)
    body = fake.statements[0]
    assert body["warehouse_id"] == "wh1" and body["catalog"] == "main"
    assert body["schema"] == "sales"  # the single schema is the default
    assert body["row_limit"] == 3 and body["on_wait_timeout"] == "CANCEL"
    assert body["disposition"] == "INLINE" and body["wait_timeout"] == "50s"
    assert rows.columns == ["region", "revenue"]
    assert rows.rows == [("North", "1200.50"), ("South", "800.00")] and rows.truncated


def test_failed_and_cancelled_statements(fake):
    fake.statement_state = "FAILED"
    with pytest.raises(AdapterError, match="TABLE_OR_VIEW_NOT_FOUND"):
        adapter(fake).run_query(CTX, signed(fake), "SELECT 1", 5)
    fake.statement_state = "CANCELED"
    with pytest.raises(AdapterError, match="did not finish in time"):
        adapter(fake).run_query(CTX, signed(fake), "SELECT 1", 5)


def test_list_and_describe_use_unity_catalog(fake):
    objects = adapter(fake).list_objects(CTX, signed(fake), None)
    assert [(o.schema, o.name, o.kind) for o in objects] == [
        ("sales", "orders", "table"), ("sales", "v_revenue", "view")]
    info = adapter(fake).describe_table(CTX, signed(fake), "sales", "orders")
    assert [(c.name, c.type, c.nullable) for c in info.columns] == [
        ("order_id", "bigint", False), ("amount", "decimal(18,2)", True)]
    assert info.comment == "One row per order"


def test_unknown_table_and_bad_token(fake):
    with pytest.raises(AdapterError, match="404"):
        adapter(fake).describe_table(CTX, signed(fake), "sales", "nope")
    with pytest.raises(CredentialRejected):
        adapter(fake).list_objects(CTX, {"access_tokens": {"all-apis": "forged"}}, None)
    with pytest.raises(AdapterError, match="No Databricks sign-in"):
        adapter(fake).run_query(CTX, {}, "SELECT 1", 1)


def test_no_warehouse_chosen(fake):
    bare = ConnectionContext(**{**CTX.__dict__, "settings": {}})
    with pytest.raises(AdapterError, match="no SQL warehouse"):
        adapter(fake).run_query(bare, signed(fake), "SELECT 1", 1)


# --- OAuth ------------------------------------------------------------------------------


def test_sign_in_link_uses_pkce_and_offline_access(dbx_app):
    url, kept = dbx_app.state.databricks_oauth.begin_link("http://localhost:8000/cb")
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{HOST}/oidc/v1/authorize"
    assert query["code_challenge_method"] == "S256" and query["scope"] == "all-apis offline_access"
    assert query["client_id"] == "iq-app" and query["state"] == kept["state"]
    assert kept["verifier"] not in url


def test_sign_in_with_wrong_state_is_refused(dbx_app):
    _, kept = dbx_app.state.databricks_oauth.begin_link("http://localhost:8000/cb")
    with pytest.raises(SignInFailed):
        dbx_app.state.databricks_oauth.finish_link(kept, {"code": "c", "state": "forged"})


def test_access_tokens_are_cached_and_rotation_returned(dbx_app, fake):
    fake.rotate = True
    oauth = dbx_app.state.databricks_oauth
    first = oauth.access_token(credential_id="c", version=1, refresh_token="dbx-rt-0")
    again = oauth.access_token(credential_id="c", version=1, refresh_token="dbx-rt-0")
    assert first.token == again.token and first.new_refresh_token == "dbx-rt-1"
    assert len(fake.token_requests) == 1


def test_dead_refresh_token_is_rejected(dbx_app, fake):
    fake.refresh_error = "invalid_grant"
    with pytest.raises(CredentialRejected):
        dbx_app.state.databricks_oauth.access_token(credential_id="c", version=1,
                                                    refresh_token="dbx-rt-0")


def test_linking_in_the_browser_stores_the_refresh_token(dbx_app):
    browser = Browser(dbx_app)
    browser.sign_in()
    start = browser.http.get("/auth/platform/databricks/start", params={"next": "/"})
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    done = browser.http.get("/auth/platform/databricks/callback",
                            params={"code": "c", "state": state})
    assert done.status_code == 303
    with dbx_app.state.db.session() as session:
        credential = session.scalars(select(Credential)).one()
        assert (credential.provider, credential.account) == ("databricks", HOST)
        assert b"dbx-rt-0" not in credential.ciphertext
        secret = read_secret(dbx_app.state.secrets, credential)
        assert secret == {"refresh_token": "dbx-rt-0", "username": "alice@acme.com"}


# --- the wizard ---------------------------------------------------------------------------


def link(browser: Browser) -> None:
    start = browser.http.get("/auth/platform/databricks/start")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    browser.http.get("/auth/platform/databricks/callback", params={"code": "c", "state": state})


def csrf_in(text: str) -> str:
    return re.search(r"name='csrf' value='([^']+)'", text).group(1)


def test_wizard_walkthrough(dbx_app):
    browser = Browser(dbx_app)
    browser.sign_in()
    assert "Choose Databricks" in browser.http.get("/app/connections/new").text
    assert "Sign in with Databricks" in browser.http.get("/app/connections/new/databricks").text
    link(browser)
    assert "Shared SQL" in browser.http.get("/app/connections/new/databricks").text
    catalogs = browser.http.get("/app/connections/new/databricks?warehouse=wh1").text
    assert "main" in catalogs and "hr" in catalogs
    form = browser.http.get("/app/connections/new/databricks?warehouse=wh1&catalog=main").text
    assert "value='sales'" in form and "value='information_schema'" not in form
    created = browser.http.post("/app/connections/new/databricks", data={
        "csrf": csrf_in(form), "warehouse": "wh1", "catalog": "main", "schemas": ["sales"],
        "name": "Lakehouse", "notes": "Amounts in USD"})
    assert created.headers["location"] == "/?done=added"
    with dbx_app.state.db.session() as session:
        row = session.scalars(select(DataConnection)).one()
        assert row.scope == {"catalog": "main", "schemas": ["sales"]}
        assert row.settings == {"warehouse_id": "wh1"} and row.credential_id is not None


@pytest.mark.parametrize(("catalog", "schemas", "expect"), [
    ("main", ["sales", "secret_schema"], "Pick schemas from the list"),
    ("not_mine", ["sales"], "isn"),
])
def test_wizard_refuses_what_you_cannot_see(dbx_app, catalog, schemas, expect):
    browser = Browser(dbx_app)
    browser.sign_in()
    link(browser)
    form = browser.http.get("/app/connections/new/databricks?warehouse=wh1&catalog=main").text
    response = browser.http.post("/app/connections/new/databricks", data={
        "csrf": csrf_in(form), "warehouse": "wh1", "catalog": catalog, "schemas": schemas,
        "name": "x", "notes": ""})
    assert expect in response.text
    with dbx_app.state.db.session() as session:
        assert session.scalars(select(DataConnection)).all() == []


# --- the full /mcp path ---------------------------------------------------------------------


def connect(app, person) -> None:
    with app.state.db.session() as session:
        user = session.get(User, person)
        credential = save_secret(session, app.state.secrets, user=user, provider="databricks",
                                 account=HOST, secret={"refresh_token": "dbx-rt-0"})
        connections.create(session, user, platform="databricks", name="Lakehouse",
                           scope=CTX.scope, settings=CTX.settings, credential_id=credential.id)


def test_query_through_mcp_runs_as_the_person(dbx_app, fake):
    alice = add_person(dbx_app, "alice@acme.com")
    connect(dbx_app, alice)
    with Mcp(dbx_app, token_for(dbx_app, alice)) as mcp:
        text, error = mcp.call("run_query", connection="Lakehouse",
                               sql="SELECT region, SUM(amount) AS revenue FROM sales.orders "
                                   "GROUP BY region")
    assert not error, text
    assert "| North | 1200.50 |" in text
    assert fake.token_requests[0]["grant_type"] == "refresh_token"
    assert fake.statements[0]["catalog"] == "main"


def test_refused_query_never_contacts_databricks(dbx_app, fake):
    alice = add_person(dbx_app, "alice@acme.com")
    connect(dbx_app, alice)
    with Mcp(dbx_app, token_for(dbx_app, alice)) as mcp:
        text, error = mcp.call("run_query", connection="Lakehouse",
                               sql="SELECT * FROM hr.people.salaries")
    assert error and "outside this connection" in text
    assert fake.token_requests == [] and fake.statements == []


def test_dead_databricks_sign_in_marks_the_connection(dbx_app, fake):
    fake.refresh_error = "invalid_grant"
    alice = add_person(dbx_app, "alice@acme.com")
    connect(dbx_app, alice)
    with Mcp(dbx_app, token_for(dbx_app, alice)) as mcp:
        text, error = mcp.call("list_objects", connection="Lakehouse")
    assert error and "sign in again" in text
    with dbx_app.state.db.session() as session:
        assert session.scalars(select(DataConnection)).one().status == "needs_signin"
