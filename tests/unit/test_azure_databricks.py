"""Azure Databricks through the linked Microsoft account (no Databricks OAuth app needed)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from browser import Browser
from fake_databricks import HOST, FakeDatabricks
from fakes import TENANT, FakeMsal
from iq_connect.adapters.databricks.adapter import AZURE_DATABRICKS_SCOPE
from iq_connect.connections import service as connections
from iq_connect.credentials.service import save_secret
from iq_connect.db.models import Base, DataConnection, User
from iq_connect.main import create_app
from iq_connect.settings import Settings
from mcp_client import Mcp, add_person, token_for


@pytest.fixture
def fake() -> FakeDatabricks:
    fake = FakeDatabricks()
    fake.accepted_prefixes = [f"at-{AZURE_DATABRICKS_SCOPE}-"]  # what FakeMsal mints
    return fake


@pytest.fixture
def azure_app(settings, fake):
    values = {**settings.model_dump(), "databricks_host": HOST, "entra_tenant": TENANT,
              "entra_client_id": "app-id", "entra_client_secret": "app-secret"}
    app = create_app(Settings(_env_file=None, **values))
    Base.metadata.create_all(app.state.db.engine)
    msal = FakeMsal()
    app.state.entra._app = msal
    app.state.pipeline.registry.get("databricks")._http = fake.client()
    return app, msal


def test_databricks_is_offered_with_the_microsoft_account(azure_app):
    app, _ = azure_app
    assert app.state.databricks_oauth is None  # no Databricks OAuth app configured
    browser = Browser(app)
    browser.sign_in()
    page = browser.http.get("/app/connections/new").text
    assert "your Microsoft account (Azure Databricks)" in page and "Choose Databricks" in page
    prompt = browser.http.get("/app/connections/new/databricks").text
    assert "Sign in with Microsoft" in prompt and "platform=databricks" in prompt


def test_linking_for_databricks_asks_microsoft_for_azure_databricks(azure_app):
    app, msal = azure_app
    browser = Browser(app)
    browser.sign_in()
    seen = {}
    original = msal.initiate_auth_code_flow

    def spy(scopes, redirect_uri, prompt=None):
        seen["scopes"] = scopes
        return original(scopes, redirect_uri, prompt)

    msal.initiate_auth_code_flow = spy
    browser.http.get("/auth/platform/entra/start", params={"platform": "databricks"})
    assert seen["scopes"] == [AZURE_DATABRICKS_SCOPE]


def test_signed_out_link_keeps_the_platform(azure_app):
    app, _ = azure_app
    browser = Browser(app)
    response = browser.http.get("/auth/platform/entra/start", params={"platform": "databricks"})
    next_path = parse_qs(urlsplit(response.headers["location"]).query)["next"][0]
    assert "platform=databricks" in next_path


def test_wizard_with_the_microsoft_account(azure_app):
    app, _ = azure_app
    browser = Browser(app)
    browser.sign_in()
    browser.http.get("/auth/platform/entra/start", params={"platform": "databricks"})
    browser.http.get("/auth/platform/entra/callback", params={"code": "c", "state": "s"})
    assert "Shared SQL" in browser.http.get("/app/connections/new/databricks").text
    form = browser.http.get("/app/connections/new/databricks?warehouse=wh1&catalog=main").text
    csrf = re.search(r"name='csrf' value='([^']+)'", form).group(1)
    created = browser.http.post("/app/connections/new/databricks", data={
        "csrf": csrf, "warehouse": "wh1", "catalog": "main", "schemas": ["sales"],
        "name": "Lakehouse", "notes": ""})
    assert created.headers["location"] == "/?done=added"
    with app.state.db.session() as session:
        row = session.scalars(select(DataConnection)).one()
        assert row.platform == "databricks" and row.credential.provider == "entra"


def test_query_uses_a_microsoft_token_for_azure_databricks(azure_app, fake):
    app, msal = azure_app
    person = add_person(app, "alice@acme.com")
    with app.state.db.session() as session:
        user = session.get(User, person)
        credential = save_secret(session, app.state.secrets, user=user, provider="entra",
                                 account=TENANT, secret={"refresh_token": "rt-0"})
        connections.create(session, user, platform="databricks", name="Lakehouse",
                           scope={"catalog": "main", "schemas": ["sales"]},
                           settings={"warehouse_id": "wh1"}, credential_id=credential.id)
    with Mcp(app, token_for(app, person)) as mcp:
        text, error = mcp.call("run_query", connection="Lakehouse",
                               sql="SELECT region, revenue FROM sales.orders")
    assert not error, text
    assert "| North | 1200.50 |" in text
    assert msal.redeemed == [("rt-0", AZURE_DATABRICKS_SCOPE)]
    assert fake.token_requests == []  # Databricks' own OAuth was never involved
