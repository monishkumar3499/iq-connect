"""data_map, profile_tables and batched run_query (ported from the prototype)."""

from __future__ import annotations

import pytest

from fake_databricks import FakeDatabricks
from fakes import FakeWarehouse
from iq_connect.adapters.base import CatalogMeta, ColumnMeta, ConnectionContext
from iq_connect.adapters.databricks.adapter import DatabricksAdapter
from iq_connect.adapters.fabric.adapter import FabricAdapter
from iq_connect.adapters.fabric.scopes import SQL_SCOPE
from iq_connect.analysis import cache, schema_map, sqlgen
from iq_connect.policy.sql_guard import check_query
from mcp_client import Mcp, add_connection, add_person, token_for


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


def star() -> CatalogMeta:
    meta = CatalogMeta()
    rows = [
        ("dim_customer", "customer_key", "int", False), ("dim_customer", "name", "varchar", True),
        ("dim_product", "product_key", "int", False),
        ("fact_sales", "sale_id", "bigint", False), ("fact_sales", "customer_key", "int", True),
        ("fact_sales", "product_key", "int", True), ("fact_sales", "order_date", "date", False),
        ("fact_sales", "load_date", "datetime2", False),
        ("fact_sales", "amount", "decimal(18,2)", True),
    ]
    for position, (table, column, kind, nullable) in enumerate(rows):
        meta.columns.append(ColumnMeta("gold", table, "TABLE", column, kind, nullable, position))
    return meta


# --- the ported analysis ------------------------------------------------------------


def test_keys_joins_and_pattern_are_inferred():
    tables = schema_map.build(star())
    fact = tables["gold.fact_sales"]
    assert fact.inferred_pk == ["sale_id"]
    assert fact.edges["customer_key"] == ("gold", "dim_customer", "customer_key")
    assert fact.edges["product_key"] == ("gold", "dim_product", "product_key")
    assert [c.name for c in fact.date_columns] == ["order_date", "load_date"]  # business first
    assert [c.name for c in fact.measure_columns] == ["amount"]
    headline, _ = schema_map.detect_pattern(tables)
    assert headline.startswith("STAR (1 fact / 2 dim")


def test_declared_keys_win_and_are_marked():
    meta = star()
    meta.primary_keys = [("gold", "dim_customer", "customer_key")]
    meta.foreign_keys = [("gold", "fact_sales", "customer_key",
                          "gold", "dim_customer", "customer_key")]
    tables = schema_map.build(meta)
    text = schema_map.render(list(tables.values()), [], full=True)
    assert "## gold.dim_customer TABLE | PK: customer_key" in text
    assert "customer_key int->gold.dim_customer(d)" in text
    assert "product_key int->gold.dim_product;" in text  # inferred: no (d)


def test_big_maps_are_summarised_only_when_that_is_shorter():
    narrow = list(schema_map.build(star()).values())
    _, was_full = schema_map.render_best(narrow, ["h"], char_budget=10_000, object_limit=60)
    assert was_full
    # Over budget, but narrow tables summarise no shorter: the full listing stays.
    _, was_full = schema_map.render_best(narrow, ["h"], char_budget=10, object_limit=60)
    assert was_full

    wide = CatalogMeta(columns=[
        ColumnMeta("gold", "fact_wide", "TABLE", f"attribute_number_{n}", "varchar", True, n)
        for n in range(40)
    ])
    summary, was_full = schema_map.render_best(list(schema_map.build(wide).values()), ["h"],
                                               char_budget=10, object_limit=60)
    assert not was_full and "(40 columns total)" in summary


# --- generated SQL ---------------------------------------------------------------------


def test_samples_use_each_dialects_limit():
    assert sqlgen.sample("sales", "orders", 5, "tsql") == "SELECT TOP 5 * FROM [sales].[orders]"
    assert sqlgen.sample("sales", "orders", 5, "databricks").endswith("LIMIT 5")


@pytest.mark.parametrize("dialect", ["tsql", "databricks", "snowflake", "sqlite"])
def test_generated_sql_passes_the_read_only_policy(dialect):
    ctx = ConnectionContext(id="c", name="c", platform="x",
                            scope={"catalog": "cat", "schemas": ["sales"]},
                            settings={}, credential_version=None)
    for sql in (sqlgen.row_counts([("sales", "orders"), ("sales", "customers")], dialect),
                sqlgen.profile("sales", "orders", ["order_id"], ["order_date"], dialect),
                sqlgen.sample("sales", "orders", 3, dialect)):
        check_query(sql, dialect, ctx)


def test_odd_names_are_quoted_not_injected():
    sql = sqlgen.profile("sales", "x]; DROP TABLE y; --", ["a]b"], [], "tsql")
    assert "[x]]; DROP TABLE y; --]" in sql and "[a]]b]" in sql


# --- adapters' catalogues -------------------------------------------------------------------


def test_fabric_catalog_reads_columns_and_declared_keys():
    ctx = ConnectionContext(id="c", name="WH", platform="fabric",
                            scope={"catalog": "SalesWH", "schemas": ["sales"]},
                            settings={"sql_endpoint": "x.fabric.microsoft.com"},
                            credential_version=1)
    meta = FabricAdapter(login_timeout=5, query_timeout=5, connect=FakeWarehouse()).catalog(
        ctx, {"access_tokens": {SQL_SCOPE: "t"}})
    assert ("sales", "dim_customer", "customer_key") in meta.primary_keys
    tables = schema_map.build(meta)
    assert tables["sales.fact_orders"].edges["customer_key"][1] == "dim_customer"
    assert "customer_key" in tables["sales.fact_orders"].declared_edges
    assert tables["sales.fact_orders"].columns[3].type_text == "decimal(18,2)"


def test_databricks_catalog_keeps_only_edges_inside_the_catalog():
    fake = FakeDatabricks()
    ctx = ConnectionContext(id="c", name="L", platform="databricks",
                            scope={"catalog": "main", "schemas": ["sales"]},
                            settings={"warehouse_id": "wh1"}, credential_version=1)
    adapter = DatabricksAdapter(host="https://x", query_timeout=30, http=fake.client)
    meta = adapter.catalog(ctx, {"access_tokens": {"all-apis": fake._issue()}})
    assert ("sales", "orders", "order_id") in meta.primary_keys
    assert meta.foreign_keys == [("sales", "orders", "customer_id",
                                  "sales", "customers", "customer_id")]
    assert {c.table for c in meta.columns} == {"orders", "v_revenue"}


# --- the tools through /mcp on the demo data -------------------------------------------


@pytest.fixture
def mcp(app):
    person = add_person(app, "alice@acme.com")
    add_connection(app, person)
    with Mcp(app, token_for(app, person)) as client:
        yield client


def test_data_map(mcp):
    text, error = mcp.call("data_map", connection="Demo sales")
    assert not error, text
    assert "pattern:" in text and "objects: 2 of 2" in text
    assert "## sales.customers TABLE | 5 rows | PK: customer_id" in text
    assert "## sales.orders TABLE | 8 rows | PK: order_id" in text
    assert "customer_id INTEGER!->sales.customers" in text  # inferred join
    assert "profile_tables" in text


def test_data_map_filter(mcp):
    text, _ = mcp.call("data_map", connection="Demo sales", tables="orders")
    assert "objects: 1 of 2" in text and "sales.customers TABLE" not in text


def test_profile_tables(mcp):
    text, error = mcp.call("profile_tables", connection="Demo sales",
                           tables=["sales.orders", "sales.customers"], sample_rows=2)
    assert not error, text
    assert "## sales.orders TABLE | 8 rows" in text
    assert "Grain: UNIQUE on order_id" in text
    assert "customer_id: 5 distinct, ~1.6 rows per value (many side)" in text
    assert "## sales.customers TABLE | 5 rows" in text
    assert "Sample (2):" in text


def test_profile_refuses_tables_outside_the_connection(mcp):
    text, error = mcp.call("profile_tables", connection="Demo sales",
                           tables="main.sqlite_master, sales.nope, sales.orders")
    assert not error
    assert "outside this connection" in text
    assert "## sales.nope\nNo such table" in text
    assert "Grain: UNIQUE on order_id" in text  # the good one still answered


def test_batched_queries_are_labelled_and_survive_a_failure(mcp):
    text, error = mcp.call("run_query", connection="Demo sales", sql=[
        "SELECT COUNT(*) AS n FROM sales.orders",
        "DELETE FROM sales.orders",
        "SELECT COUNT(*) AS n FROM sales.customers",
    ])
    assert not error
    assert "[1/3]\n| n |" in text and "| 8 |" in text
    assert "[2/3]\nFAILED: " in text and "read-only" in text
    assert "[3/3]\n| n |" in text and "| 5 |" in text


def test_single_bad_query_is_still_an_error(mcp):
    _, error = mcp.call("run_query", connection="Demo sales", sql="DELETE FROM sales.orders")
    assert error
