"""The dashboard: shows only your own things, and every change needs your CSRF token."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from browser import Browser
from iq_connect.db.models import DataConnection, User
from mcp_client import Mcp, add_connection, token_for
from oauth_client import FakeClaude


def person_id(app, email: str):
    with app.state.db.session() as session:
        return session.scalars(select(User.id).where(User.email == email)).one()


@pytest.fixture
def alice(app):
    browser = Browser(app)
    browser.sign_in("alice@example.com")
    return browser


def connection_id(app, name: str):
    with app.state.db.session() as session:
        return session.scalars(
            select(DataConnection.id).where(DataConnection.name == name)
        ).one()


def test_signed_out_visitors_see_the_landing_page(client):
    text = client.get("/").text
    assert "Sign in" in text
    assert "http://localhost:8000/mcp" in text
    assert "Your connections" not in text


def test_new_person_sees_empty_states(alice):
    text = alice.http.get("/").text
    assert "No connections yet" in text
    assert "Nothing yet" in text
    assert "No app is connected yet" in text
    assert "alice@example.com" in text


def test_shows_connections_activity_and_apps(app, alice):
    me = person_id(app, "alice@example.com")
    add_connection(app, me, custom_prompt="Amounts are in USD.")
    with Mcp(app, token_for(app, me)) as mcp:
        mcp.call("run_query", connection="Demo sales", sql="SELECT * FROM sales.customers")
        mcp.call("run_query", connection="Demo sales", sql="DELETE FROM sales.orders")
    text = alice.http.get("/").text
    assert "Demo sales" in text and "Amounts are in USD." in text
    assert "SELECT * FROM sales.customers" in text
    assert "DELETE FROM sales.orders" in text and "read-only" in text
    assert "<span class='pill warn'>rejected</span>" in text
    assert "Claude" in text and "Disconnect" in text


def test_never_shows_someone_elses_things(app, alice):
    bob = Browser(app)
    bob.sign_in("bob@example.com")
    bob_id = person_id(app, "bob@example.com")
    add_connection(app, bob_id, name="Bob secret", custom_prompt="bob only")
    with Mcp(app, token_for(app, bob_id)) as mcp:
        mcp.call("run_query", connection="Bob secret", sql="SELECT 'bob query'")
    text = alice.http.get("/").text
    assert "Bob secret" not in text and "bob only" not in text and "bob query" not in text


def test_edit_name_and_notes(app, alice):
    me = person_id(app, "alice@example.com")
    add_connection(app, me)
    cid = connection_id(app, "Demo sales")
    response = alice.http.post(f"/app/connections/{cid}/edit", data={
        "name": "Sales sample", "notes": "Fiscal year starts in April.",
        "csrf": alice.csrf_on("/")})
    assert response.headers["location"] == "/?done=saved"
    text = alice.http.get("/?done=saved").text
    assert "Saved." in text and "Sales sample" in text and "Fiscal year starts in April." in text


def test_edit_refuses_a_duplicate_or_empty_name(app, alice):
    me = person_id(app, "alice@example.com")
    add_connection(app, me, name="One")
    add_connection(app, me, name="Two")
    csrf = alice.csrf_on("/")
    cid = connection_id(app, "Two")
    taken = alice.http.post(f"/app/connections/{cid}/edit",
                            data={"name": "one", "notes": "", "csrf": csrf})
    assert taken.headers["location"] == "/?problem=name_taken"
    empty = alice.http.post(f"/app/connections/{cid}/edit",
                            data={"name": "  ", "notes": "", "csrf": csrf})
    assert empty.headers["location"] == "/?problem=name_empty"


def test_cannot_edit_or_delete_someone_elses_connection(app, alice):
    bob = Browser(app)
    bob.sign_in("bob@example.com")
    add_connection(app, person_id(app, "bob@example.com"), name="Bob secret")
    cid = connection_id(app, "Bob secret")
    csrf = alice.csrf_on("/")
    edited = alice.http.post(f"/app/connections/{cid}/edit",
                             data={"name": "pwned", "notes": "", "csrf": csrf})
    deleted = alice.http.post(f"/app/connections/{cid}/delete", data={"csrf": csrf})
    assert edited.headers["location"] == deleted.headers["location"] == "/?problem=not_found"
    with app.state.db.session() as session:
        assert session.get(DataConnection, cid).name == "Bob secret"


def test_delete(app, alice):
    add_connection(app, person_id(app, "alice@example.com"))
    cid = connection_id(app, "Demo sales")
    alice.http.post(f"/app/connections/{cid}/delete", data={"csrf": alice.csrf_on("/")})
    assert "No connections yet" in alice.http.get("/").text


@pytest.mark.parametrize("path", [
    "/app/demo-connection", "/app/apps/disconnect",
    "/app/connections/00000000-0000-0000-0000-000000000000/delete",
])
def test_every_action_needs_the_csrf_token(alice, path):
    assert alice.http.post(path, data={"csrf": "forged"}).status_code == 403
    assert alice.http.post(path, data={}).status_code == 403


def test_actions_need_a_session(client):
    assert client.post("/app/demo-connection", data={"csrf": "x"}).status_code == 401


def test_add_demo_connection_twice_gets_two_names(app, alice):
    alice.http.post("/app/demo-connection", data={"csrf": alice.csrf_on("/")})
    alice.http.post("/app/demo-connection", data={"csrf": alice.csrf_on("/")})
    text = alice.http.get("/").text
    assert "Demo sales" in text and "Demo sales 2" in text


def test_disconnect_revokes_claudes_access(app, alice):
    me = person_id(app, "alice@example.com")
    claude = FakeClaude(app)
    claude.register()
    tokens = claude.sign_in(me)
    alice.http.post("/app/apps/disconnect",
                    data={"client_id": claude.client_id, "csrf": alice.csrf_on("/")})
    assert claude.refresh(tokens["refresh_token"]).json()["error"] == "invalid_grant"
    assert "No app is connected yet" in alice.http.get("/").text


def test_messages_come_only_from_fixed_codes(alice):
    text = alice.http.get("/?done=<script>alert(1)</script>&problem=evil").text
    assert "<script>alert(1)" not in text
    assert "notice" not in text.split("<main", 1)[1].split("tiles", 1)[0]


def test_notes_are_escaped(app, alice):
    add_connection(app, person_id(app, "alice@example.com"),
                   custom_prompt="<img src=x onerror=alert(1)>")
    text = alice.http.get("/").text
    assert "<img src=x" not in text and "&lt;img src=x" in text
