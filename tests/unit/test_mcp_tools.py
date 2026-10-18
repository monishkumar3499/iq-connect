"""The tools, through the whole pipeline, against the demo platform."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from iq_connect.db.models import AuditEvent, DataConnection
from mcp_client import Mcp, add_connection, add_person, token_for


@pytest.fixture
def alice(app):
    person = add_person(app, "alice@acme.com")
    add_connection(app, person, custom_prompt="Amounts are in USD.")
    return person


@pytest.fixture
def mcp(app, alice):
    with Mcp(app, token_for(app, alice)) as client:
        yield client


def audit_rows(app) -> list[AuditEvent]:
    with app.state.db.session() as session:
        return list(session.scalars(select(AuditEvent).order_by(AuditEvent.id)))


# --- happy paths --------------------------------------------------------------


def test_list_connections_shows_dialect_scope_and_notes(mcp):
    text, error = mcp.call("list_connections")
    assert not error
    assert "Demo sales" in text
    assert "SQL dialect: sqlite" in text
    assert "schemas sales" in text
    assert "Amounts are in USD." in text


def test_list_connections_when_there_are_none(app):
    person = add_person(app, "bob@acme.com")
    with Mcp(app, token_for(app, person)) as client:
        text, error = client.call("list_connections")
    assert not error
    assert "no data connections yet" in text


def test_list_objects(mcp):
    text, error = mcp.call("list_objects", connection="Demo sales")
    assert not error
    assert "sales.customers (table)" in text and "sales.orders (table)" in text


def test_describe_table(mcp):
    text, error = mcp.call("describe_table", connection="demo SALES", table="orders")
    assert not error
    assert "sales.orders" in text
    assert "- amount: REAL, not null" in text
    assert "Notes for 'Demo sales': Amounts are in USD." in text


def test_run_query_returns_a_table_and_the_notes(mcp):
    text, error = mcp.call(
        "run_query", connection="Demo sales",
        sql="SELECT status, COUNT(*) AS n FROM sales.orders GROUP BY status ORDER BY status",
    )
    assert not error
    assert text.startswith("Notes for 'Demo sales': Amounts are in USD.")
    assert "| status | n |" in text
    assert "| shipped | 5 |" in text
    assert "3 rows." in text


def test_max_rows_truncates_and_says_so(mcp):
    text, error = mcp.call("run_query", connection="Demo sales",
                           sql="SELECT order_id FROM sales.orders", max_rows=3)
    assert not error
    assert text.count("\n| 10") == 3
    assert "more exist" in text


def test_answers_are_not_sent_twice(mcp):
    response = mcp.post("tools/call", {"name": "list_connections", "arguments": {}})
    assert "structuredContent" not in response.json()["result"]


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM sales.orders",
        "SELECT 1; DROP TABLE sales.orders",
        "SELECT * FROM main.sqlite_master",
        "SELECT * INTO sales.copy FROM sales.orders",
    ],
)
def test_run_query_refuses_anything_but_an_in_scope_select(mcp, sql):
    text, error = mcp.call("run_query", connection="Demo sales", sql=sql)
    assert error
    assert "Query rejected" in text


def test_describe_outside_scope_is_refused(mcp):
    text, error = mcp.call("describe_table", connection="Demo sales", table="main.sqlite_master")
    assert error and "outside this connection" in text


def test_sql_errors_come_back_readably(mcp):
    text, error = mcp.call("run_query", connection="Demo sales",
                           sql="SELECT no_such_column FROM sales.orders")
    assert error
    assert "no such column" in text


def test_unknown_connection_lists_the_real_ones(mcp):
    text, error = mcp.call("run_query", connection="Finance", sql="SELECT 1")
    assert error
    assert "no connection named 'Finance'" in text
    assert "'Demo sales'" in text


def test_someone_elses_connection_is_invisible(app, alice):
    # Bob has a connection with a name Alice can guess; she still can't use it.
    bob = add_person(app, "bob@acme.com")
    add_connection(app, bob, name="Bob private")
    with Mcp(app, token_for(app, alice)) as client:
        text, error = client.call("run_query", connection="Bob private",
                                  sql="SELECT * FROM sales.orders")
        listing, _ = client.call("list_connections")
    assert error and "no connection named" in text
    assert "Bob private" not in listing


def test_same_name_resolves_to_each_persons_own_connection(app, alice):
    bob = add_person(app, "bob@acme.com")
    add_connection(app, bob, name="Demo sales", custom_prompt="Bob's notes")
    with Mcp(app, token_for(app, alice)) as client:
        text, _ = client.call("describe_table", connection="Demo sales", table="orders")
    assert "Amounts are in USD." in text and "Bob's notes" not in text


def test_connection_needing_sign_in_is_refused(app, alice, mcp):
    with app.state.db.session() as session:
        row = session.scalars(select(DataConnection)).one()
        row.status = "needs_signin"
    text, error = mcp.call("run_query", connection="Demo sales", sql="SELECT 1")
    assert error and "sign in" in text


def test_platform_not_offered_is_refused(app, alice, mcp):
    add_connection(app, alice, name="Warehouse", platform="snowflake")
    text, error = mcp.call("run_query", connection="Warehouse", sql="SELECT 1")
    assert error and "does not offer" in text


def test_removed_account_is_told_to_reconnect(app, alice, mcp):
    from iq_connect.db.models import User

    with app.state.db.session() as session:
        session.delete(session.get(User, alice))
    text, error = mcp.call("list_objects", connection="Demo sales")
    assert error and "Reconnect" in text


# --- audit ------------------------------------------------------------------


def test_every_call_is_audited(app, alice, mcp):
    mcp.call("list_connections")
    mcp.call("run_query", connection="Demo sales", sql="SELECT * FROM sales.customers")
    mcp.call("run_query", connection="Demo sales", sql="DELETE FROM sales.orders")
    mcp.call("run_query", connection="Demo sales", sql="SELECT nope FROM sales.orders")
    rows = audit_rows(app)
    assert [(r.tool, r.outcome) for r in rows] == [
        ("list_connections", "ok"),
        ("run_query", "ok"),
        ("run_query", "rejected"),
        ("run_query", "error"),
    ]
    assert rows[1].row_count == 5
    assert rows[1].statement == "SELECT * FROM sales.customers"
    assert all(r.user_id == alice for r in rows)
    assert rows[2].error and "read-only" in rows[2].error
