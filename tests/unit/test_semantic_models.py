"""Fabric semantic models: the DAX policy, Power BI calls, scoping, tools and wizard."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from sqlalchemy import select

from browser import Browser
from fakes import TENANT, FakeMsal, FakeWarehouse
from iq_connect.adapters.fabric.scopes import POWERBI_SCOPE, SQL_SCOPE
from iq_connect.connections import service as connections
from iq_connect.credentials.service import save_secret
from iq_connect.db.models import Base, DataConnection, User
from iq_connect.main import create_app
from iq_connect.policy.dax_guard import check_dax
from iq_connect.policy.sql_guard import QueryRejected
from iq_connect.settings import Settings
from mcp_client import Mcp, add_person, token_for

MODELS = [{"id": "m-1", "name": "Sales Model"}, {"id": "m-2", "name": "HR Model"}]

# --- the DAX policy ---------------------------------------------------------------


@pytest.mark.parametrize("dax", [
    'EVALUATE SUMMARIZECOLUMNS(dim_customer[segment], "Revenue", [Total Revenue])',
    "  evaluate ROW(\"x\", 1)",
    'DEFINE MEASURE Sales[Ad hoc] = SUM(Sales[Amount]) EVALUATE ROW("v", [Ad hoc])',
    '-- a comment\nEVALUATE ROW("x", 1)',
])
def test_allowed_dax(dax):
    check_dax(dax)


@pytest.mark.parametrize("dax", [
    "", "SELECT 1", "CREATEORREPLACE TABLE x", "EVALUATE ROW(\"x\", 1) /* */ refresh",
    "/* EVALUATE */ DELETE something", "ALTER MODEL x",
])
def test_refused_dax(dax):
    with pytest.raises(QueryRejected):
        check_dax(dax)


# --- a whole app with a fake Power BI ------------------------------------------------


class FakePowerBI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []  # (dataset id, dax, token)
        self.fail_info_view = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        dataset = request.url.path.split("/datasets/")[1].split("/")[0]
        dax = json.loads(request.content)["queries"][0]["query"]
        self.calls.append((dataset, dax, request.headers["authorization"]))
        if "INFO.VIEW.MEASURES" in dax and self.fail_info_view:
            return httpx.Response(400, json={"error": {"message": "INFO.VIEW unavailable"}})
        if "MEASURES" in dax:
            rows = [{"[Measure]": "Total Revenue", "[Expression]": "SUM(Sales[Amount])"}]
        elif "TABLES" in dax:
            rows = [{"[Table]": "Sales"}, {"[Table]": "Customer"}]
        elif "RELATIONSHIPS" in dax:
            rows = [{"[Relationship]": "Sales[CustKey] -> Customer[CustKey]",
                     "[Active]": True}]
        else:
            rows = [{"Customer[Segment]": "Enterprise", "[Revenue]": 1200.5},
                    {"Customer[Segment]": "SMB", "[Revenue]": 300}]
        return httpx.Response(200, json={"results": [{"tables": [{"rows": rows}]}]})


@pytest.fixture
def powerbi() -> FakePowerBI:
    return FakePowerBI()


@pytest.fixture
def fabric(settings, powerbi):
    values = {**settings.model_dump(), "entra_tenant": TENANT,
              "entra_client_id": "app-id", "entra_client_secret": "app-secret"}
    app = create_app(Settings(_env_file=None, **values))
    Base.metadata.create_all(app.state.db.engine)
    msal = FakeMsal()
    app.state.entra._app = msal
    adapter = app.state.pipeline.registry.get("fabric")
    adapter._connect = FakeWarehouse()
    adapter._http = httpx.Client(transport=httpx.MockTransport(powerbi.handle))
    return app, msal


def connect(app, person, models=MODELS[:1]) -> None:
    with app.state.db.session() as session:
        user = session.get(User, person)
        credential = save_secret(session, app.state.secrets, user=user, provider="entra",
                                 account=TENANT, secret={"refresh_token": "rt-0"})
        connections.create(session, user, platform="fabric", name="Sales WH",
                           scope={"catalog": "SalesWH", "schemas": ["sales"]},
                           settings={"workspace_id": "w2", "sql_endpoint": "abc.fabric",
                                     "semantic_models": models},
                           credential_id=credential.id)


@pytest.fixture
def mcp(fabric):
    app, _ = fabric
    person = add_person(app, "alice@acme.com")
    connect(app, person)
    with Mcp(app, token_for(app, person)) as client:
        yield client


def test_model_map_shows_measures_tables_relationships(mcp, powerbi):
    text, error = mcp.call("semantic_model_map", connection="Sales WH")
    assert not error, text
    assert "### Semantic model: Sales Model" in text
    assert "Total Revenue" in text and "SUM(Sales[Amount])" in text
    assert "**Relationships** (1)" in text
    assert {call[0] for call in powerbi.calls} == {"m-1"}


def test_model_map_falls_back_when_info_view_is_missing(mcp, powerbi):
    powerbi.fail_info_view = True
    text, error = mcp.call("semantic_model_map", connection="Sales WH")
    assert not error
    assert "Total Revenue" in text
    assert any("INFO.MEASURES()" in dax for _, dax, _ in powerbi.calls)


def test_query_semantic_model(mcp, powerbi):
    text, error = mcp.call("query_semantic_model", connection="Sales WH",
                           dax='EVALUATE SUMMARIZECOLUMNS(Customer[Segment], '
                               '"Revenue", [Total Revenue])')
    assert not error, text
    assert "| Enterprise | 1200.5 |" in text
    assert powerbi.calls[-1][2].startswith("Bearer at-" + POWERBI_SCOPE)


def test_batched_dax_survives_a_refusal(mcp):
    text, error = mcp.call("query_semantic_model", connection="Sales WH",
                           dax=['EVALUATE ROW("x", 1)', "ALTER MODEL nope"])
    assert not error
    assert "[1/2]" in text and "[2/2]\nFAILED: " in text and "EVALUATE" in text


def test_only_the_connections_models_can_be_used(mcp, powerbi):
    text, error = mcp.call("query_semantic_model", connection="Sales WH",
                           dax='EVALUATE ROW("x", 1)', model="HR Model")
    assert error and "not one of this connection's semantic models" in text
    _, error = mcp.call("query_semantic_model", connection="Sales WH",
                        dax='EVALUATE ROW("x", 1)', model="m-2")
    assert error
    assert powerbi.calls == []


def test_refused_dax_never_contacts_microsoft(fabric, mcp, powerbi):
    _, msal = fabric
    _, error = mcp.call("query_semantic_model", connection="Sales WH", dax="DELETE x")
    assert error and powerbi.calls == [] and msal.redeemed == []


def test_sql_calls_never_ask_for_power_bi(fabric, mcp):
    _, msal = fabric
    mcp.call("run_query", connection="Sales WH", sql="SELECT 1")
    assert {scope for _, scope in msal.redeemed} == {SQL_SCOPE}


def test_list_connections_mentions_the_models(mcp):
    text, _ = mcp.call("list_connections")
    assert "semantic models Sales Model" in text


def test_demo_connections_have_no_semantic_models(app):
    from mcp_client import add_connection

    person = add_person(app, "bob@acme.com")
    add_connection(app, person)
    with Mcp(app, token_for(app, person)) as client:
        text, error = client.call("semantic_model_map", connection="Demo sales")
    assert error and "no semantic models" in text


# --- the wizard -------------------------------------------------------------------------


class FakeApi:
    def __init__(self, token: str) -> None:
        self.token = token

    def workspaces(self):
        return [{"id": "w2", "name": "Sales"}]

    def warehouses(self, workspace_id):
        return [{"id": "h1", "name": "SalesWH", "sql_endpoint": "abc.fabric.microsoft.com"}]

    def semantic_models(self, workspace_id):
        return list(MODELS)


def test_wizard_lets_you_pick_semantic_models(fabric):
    app, _ = fabric
    app.state.fabric_api = FakeApi
    browser = Browser(app)
    browser.sign_in()
    browser.http.get("/auth/platform/entra/start")
    browser.http.get("/auth/platform/entra/callback", params={"code": "c", "state": "s"})
    form = browser.http.get("/app/connections/new/fabric?workspace=w2&warehouse=h1").text
    assert "value='m-1'" in form and "Sales Model" in form
    csrf = re.search(r"name='csrf' value='([^']+)'", form).group(1)

    sneaky = browser.http.post("/app/connections/new/fabric", data={
        "csrf": csrf, "workspace": "w2", "warehouse": "h1", "schemas": ["sales"],
        "models": ["m-1", "m-9"], "name": "x", "notes": ""})
    assert "Pick semantic models from the list" in sneaky.text

    browser.http.post("/app/connections/new/fabric", data={
        "csrf": csrf, "workspace": "w2", "warehouse": "h1", "schemas": ["sales"],
        "models": ["m-1"], "name": "Sales WH", "notes": ""})
    with app.state.db.session() as session:
        row = session.scalars(select(DataConnection)).one()
        assert row.settings["semantic_models"] == [{"id": "m-1", "name": "Sales Model"}]
