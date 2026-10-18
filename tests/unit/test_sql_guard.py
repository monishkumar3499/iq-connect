"""
The read-only SQL policy. Step 7 grows this into a full adversarial suite per
dialect; a change that lets a refused query through is a regression.
"""

from __future__ import annotations

import pytest

from iq_connect.adapters.base import ConnectionContext
from iq_connect.policy.sql_guard import QueryRejected, check_query, resolve_table_name

CONN = ConnectionContext(
    id="c", name="Sales", platform="snowflake",
    scope={"catalog": "ANALYTICS", "schemas": ["MART", "SALES"]},
    settings={}, credential_version=None,
)
SINGLE = ConnectionContext(
    id="c", name="Sales", platform="snowflake",
    scope={"catalog": "ANALYTICS", "schemas": ["MART"]}, settings={}, credential_version=None,
)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM mart.orders",
        "select o.id, c.name from mart.orders o join sales.customers c on c.id = o.customer_id",
        "SELECT * FROM analytics.mart.orders",
        "WITH recent AS (SELECT * FROM mart.orders) SELECT COUNT(*) FROM recent",
        "SELECT * FROM mart.a UNION ALL SELECT * FROM sales.b",
        "SELECT * FROM (SELECT id FROM mart.orders) t",
        "SELECT 1",
        "SELECT * FROM mart.orders WHERE id IN (SELECT id FROM sales.refunds)",
        "SELECT 'DROP TABLE x; --' AS harmless_text FROM mart.orders",
    ],
)
def test_allowed(sql):
    check_query(sql, "snowflake", CONN)


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("DELETE FROM mart.orders", "Only SELECT"),
        ("UPDATE mart.orders SET x = 1", "Only SELECT"),
        ("INSERT INTO mart.orders VALUES (1)", "Only SELECT"),
        ("DROP TABLE mart.orders", "Only SELECT"),
        ("CREATE TABLE mart.x AS SELECT 1", "Only SELECT"),
        ("MERGE INTO mart.a USING mart.b ON a.id = b.id WHEN MATCHED THEN DELETE", "Only SELECT"),
        ("GRANT SELECT ON mart.orders TO ROLE public", "Only SELECT"),
        ("USE ROLE accountadmin", "Only SELECT"),
        ("CALL system$cancel_all_queries()", "Only SELECT"),
        ("SELECT 1; DELETE FROM mart.orders", "exactly one statement"),
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("", "empty"),
        ("SELECT * FROM finance.payroll", "outside this connection"),
        ("SELECT * FROM other_db.mart.orders", "outside this connection"),
        ("SELECT * FROM mart.orders o JOIN finance.payroll p ON 1=1", "outside this connection"),
        ("SELECT * FROM mart.a WHERE id IN (SELECT id FROM finance.payroll)", "outside"),
        ("WITH x AS (SELECT * FROM finance.payroll) SELECT * FROM x", "outside this connection"),
        ("SELECT * FROM orders", "Qualify orders"),
        ("SELECT * FROM table(result_scan(last_query_id()))", "table functions"),
        ("SELECT * FROM mart.orders, LATERAL FLATTEN(input => tags)", "not allowed"),
    ],
)
def test_refused(sql, why):
    with pytest.raises(QueryRejected, match=why):
        check_query(sql, "snowflake", CONN)


def test_file_readers_are_refused():
    with pytest.raises(QueryRejected, match="table functions"):
        check_query("SELECT * FROM read_files('s3://bucket/x')", "databricks", CONN)


def test_select_into_is_refused():
    with pytest.raises(QueryRejected, match="INTO"):
        check_query("SELECT * INTO mart.copy FROM mart.orders", "tsql", CONN)


def test_unparseable_sql_is_refused():
    with pytest.raises(QueryRejected, match="could not be parsed"):
        check_query("SELECT * FROM OPENROWSET(BULK 'c:/secrets.txt') AS r", "tsql", CONN)


def test_unqualified_name_uses_the_single_schema():
    assert [str(t) for t in check_query("SELECT * FROM orders", "snowflake", SINGLE)] == [
        "MART.orders"
    ]


def test_names_compare_without_case():
    check_query('SELECT * FROM Analytics.Mart.Orders', "snowflake", CONN)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("mart.orders", ("mart", "orders")), ("ANALYTICS.mart.orders", ("mart", "orders"))],
)
def test_resolve_table_name(name, expected):
    assert resolve_table_name(CONN, name) == expected


@pytest.mark.parametrize("name", ["finance.payroll", "other.mart.orders", "orders", "a.b.c.d", ""])
def test_resolve_table_name_refuses(name):
    with pytest.raises(QueryRejected):
        resolve_table_name(CONN, name)
