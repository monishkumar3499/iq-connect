"""The New connection wizard, including from inside Claude's sign-in."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from browser import Browser
from fakes import TENANT, FakeMsal, FakeWarehouse
from iq_connect.db.models import Base, DataConnection
from iq_connect.main import create_app
from iq_connect.settings import Settings
from oauth_client import CLAUDE_CALLBACK, FakeClaude, query


class FakeApi:
    def __init__(self, token: str) -> None:
        self.token = token

    def workspaces(self):
        return [{"id": "w1", "name": "Finance"}, {"id": "w2", "name": "Sales"}]

    def warehouses(self, workspace_id):
        if workspace_id != "w2":
            return []
        return [{"id": "h1", "name": "SalesWH", "sql_endpoint": "abc.fabric.microsoft.com"}]


@pytest.fixture
def fabric_app(settings):
    values = {**settings.model_dump(), "entra_tenant": TENANT,
              "entra_client_id": "app-id", "entra_client_secret": "app-secret"}
    app = create_app(Settings(_env_file=None, **values))
    Base.metadata.create_all(app.state.db.engine)
    app.state.entra._app = FakeMsal()
    app.state.pipeline.registry.get("fabric")._connect = FakeWarehouse()
    app.state.fabric_api = FakeApi
    return app


def connections_of(app) -> list[DataConnection]:
    with app.state.db.session() as session:
        return list(session.scalars(select(DataConnection)))


def csrf_in(text: str) -> str:
    return re.search(r"name='csrf' value='([^']+)'", text).group(1)


# --- the platform step -----------------------------------------------------------------


def test_signed_out_goes_to_sign_in_and_comes_back(app):
    browser = Browser(app)
    response = browser.http.get("/app/connections/new")
    assert response.headers["location"].startswith("/auth/login?")
    assert browser.sign_in(next_path="/app/connections/new") == "/app/connections/new"


def test_platform_choices(fabric_app):
    browser = Browser(fabric_app)
    browser.sign_in()
    text = browser.http.get("/app/connections/new").text
    assert "Choose Microsoft Fabric" in text and "your Microsoft account (SSO)" in text
    assert "Databricks" in text and "Snowflake" in text and "not available yet" in text
    assert "Choose Demo" in text


def test_fabric_unavailable_without_entra_settings(app):
    browser = Browser(app)
    browser.sign_in()
    text = browser.http.get("/app/connections/new").text
    assert "Microsoft (Entra) settings" in text
    assert browser.http.get("/app/connections/new/fabric").status_code == 404


# --- demo ------------------------------------------------------------------------------


def test_create_demo_connection(app):
    browser = Browser(app)
    browser.sign_in()
    page = browser.http.get("/app/connections/new/demo").text
    response = browser.http.post("/app/connections/new/demo", data={
        "name": "Sample", "notes": "USD", "csrf": csrf_in(page)})
    assert response.headers["location"] == "/?done=added"
    [row] = connections_of(app)
    assert (row.name, row.platform, row.custom_prompt) == ("Sample", "demo", "USD")
    assert row.scope == {"catalog": "demo", "schemas": ["sales"]}


def test_duplicate_name_is_explained(app):
    browser = Browser(app)
    browser.sign_in()
    for _ in range(2):
        page = browser.http.get("/app/connections/new/demo").text
        response = browser.http.post("/app/connections/new/demo", data={
            "name": "Sample", "notes": "", "csrf": csrf_in(page)})
    assert response.status_code == 200
    assert "already have a connection with that name" in response.text
    assert len(connections_of(app)) == 1


def test_create_needs_the_csrf_token(app):
    browser = Browser(app)
    browser.sign_in()
    response = browser.http.post("/app/connections/new/demo",
                                 data={"name": "x", "notes": "", "csrf": "forged"})
    assert response.status_code == 403
    assert connections_of(app) == []


def test_bad_flow_values_are_dropped(app):
    browser = Browser(app)
    browser.sign_in()
    page = browser.http.get("/app/connections/new/demo?flow=//evil.example").text
    assert "evil.example" not in page
    response = browser.http.post("/app/connections/new/demo", data={
        "name": "x", "notes": "", "csrf": csrf_in(page), "flow": "https://evil.example"})
    assert response.headers["location"] == "/?done=added"


# --- inside Claude's sign-in --------------------------------------------------------------


def test_from_claude_sign_in_to_new_connection_and_back(app):
    claude = FakeClaude(app)
    claude.register()
    connect_url = claude.authorize().headers["location"]
    flow = query(connect_url)["flow"]
    browser = Browser(app)
    browser.sign_in(next_path=connect_url)

    connect_page = browser.http.get(connect_url).text
    assert "Create your first connection" in connect_page
    wizard_url = re.search(r"href='(/app/connections/new\?flow=[^']+)'", connect_page).group(1)
    wizard_url = wizard_url.replace("&amp;", "&")
    assert flow in wizard_url

    platforms = browser.http.get(wizard_url).text
    demo_url = re.search(r"href='(/app/connections/new/demo[^']*)'", platforms).group(1)
    demo_page = browser.http.get(demo_url.replace("&amp;", "&")).text
    created = browser.http.post("/app/connections/new/demo", data={
        "name": "Demo sales", "notes": "", "csrf": csrf_in(demo_page), "flow": flow})
    assert created.headers["location"] == f"/connect?flow={flow}"

    back = browser.http.get(created.headers["location"]).text
    assert "Demo sales" in back and "Allow" in back
    allowed = browser.http.post("/connect/approve", data={"flow": flow, "csrf": csrf_in(back)})
    assert allowed.headers["location"].startswith(CLAUDE_CALLBACK)
    assert claude.exchange(query(allowed.headers["location"])["code"]).status_code == 200


# --- Fabric --------------------------------------------------------------------------------


def link_microsoft(browser: Browser) -> None:
    browser.http.get("/auth/platform/entra/start")
    browser.http.get("/auth/platform/entra/callback", params={"code": "c", "state": "s"})


def test_fabric_asks_to_link_first_and_returns_to_the_wizard(fabric_app):
    browser = Browser(fabric_app)
    browser.sign_in()
    page = browser.http.get("/app/connections/new/fabric?flow=" + "f" * 30).text
    assert "Sign in with Microsoft" in page and "No password is stored" in page
    link = re.search(r"href='(/auth/platform/entra/start\?[^']+)'", page).group(1)
    next_path = parse_qs(urlsplit(link.replace("&amp;", "&")).query)["next"][0]
    assert next_path == "/app/connections/new/fabric?flow=" + "f" * 30


def test_fabric_walkthrough_creates_a_scoped_connection(fabric_app):
    browser = Browser(fabric_app)
    browser.sign_in()
    link_microsoft(browser)

    workspaces = browser.http.get("/app/connections/new/fabric").text
    assert "Finance" in workspaces and "Sales" in workspaces
    warehouses = browser.http.get("/app/connections/new/fabric?workspace=w2").text
    assert "SalesWH" in warehouses
    form = browser.http.get("/app/connections/new/fabric?workspace=w2&warehouse=h1").text
    assert "value='sales'" in form and "value='dbo'" in form and "value='sys'" not in form

    response = browser.http.post("/app/connections/new/fabric", data={
        "csrf": csrf_in(form), "workspace": "w2", "warehouse": "h1",
        "schemas": ["sales"], "name": "Sales WH", "notes": "EUR",
        # Anything else in the form is ignored: address and name come from Fabric.
        "sql_endpoint": "evil.example", "catalog": "master",
    })
    assert response.headers["location"] == "/?done=added"
    [row] = connections_of(fabric_app)
    assert row.platform == "fabric" and row.credential_id is not None
    assert row.scope == {"catalog": "SalesWH", "schemas": ["sales"]}
    assert row.settings == {"workspace_id": "w2", "warehouse_id": "h1",
                            "sql_endpoint": "abc.fabric.microsoft.com",
                            "semantic_models": []}


def test_fabric_refuses_schemas_that_are_not_there(fabric_app):
    browser = Browser(fabric_app)
    browser.sign_in()
    link_microsoft(browser)
    form = browser.http.get("/app/connections/new/fabric?workspace=w2&warehouse=h1").text
    response = browser.http.post("/app/connections/new/fabric", data={
        "csrf": csrf_in(form), "workspace": "w2", "warehouse": "h1",
        "schemas": ["sales", "sys"], "name": "x", "notes": ""})
    assert "Pick schemas from the list" in response.text
    assert connections_of(fabric_app) == []


def test_fabric_refuses_a_warehouse_you_cannot_see(fabric_app):
    browser = Browser(fabric_app)
    browser.sign_in()
    link_microsoft(browser)
    assert browser.http.get(
        "/app/connections/new/fabric?workspace=w2&warehouse=other").status_code == 404
    form = browser.http.get("/app/connections/new/fabric?workspace=w2&warehouse=h1").text
    response = browser.http.post("/app/connections/new/fabric", data={
        "csrf": csrf_in(form), "workspace": "w1", "warehouse": "h1",
        "schemas": ["sales"], "name": "x", "notes": ""})
    assert response.status_code == 404
    assert connections_of(fabric_app) == []


def test_fabric_needs_at_least_one_schema(fabric_app):
    browser = Browser(fabric_app)
    browser.sign_in()
    link_microsoft(browser)
    form = browser.http.get("/app/connections/new/fabric?workspace=w2&warehouse=h1").text
    response = browser.http.post("/app/connections/new/fabric", data={
        "csrf": csrf_in(form), "workspace": "w2", "warehouse": "h1", "name": "x", "notes": ""})
    assert "Pick at least one schema" in response.text


def test_dead_microsoft_link_asks_to_sign_in_again(fabric_app):
    browser = Browser(fabric_app)
    browser.sign_in()
    link_microsoft(browser)
    fabric_app.state.entra._app.fail_with = "invalid_grant"
    page = browser.http.get("/app/connections/new/fabric").text
    assert "no longer accepts your linked account" in page
