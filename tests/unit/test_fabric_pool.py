"""Fabric connection reuse: reused only when safe, never kept when not."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fakes import FakeWarehouse
from iq_connect.adapters.base import AdapterError, ConnectionContext
from iq_connect.adapters.fabric.adapter import FabricAdapter
from iq_connect.adapters.fabric.pool import FabricConnectionPool, token_fingerprint
from iq_connect.adapters.fabric.scopes import SQL_SCOPE

CTX = ConnectionContext(
    id="c1", name="Sales WH", platform="fabric",
    scope={"catalog": "SalesWH", "schemas": ["sales"]},
    settings={"sql_endpoint": "abc.datawarehouse.fabric.microsoft.com"},
    credential_version=1,
)


def secret(token: str = "token-1") -> dict:
    return {"access_tokens": {SQL_SCOPE: token}}


class Clock:
    def __init__(self) -> None:
        self.time = 1000.0

    def __call__(self) -> float:
        return self.time


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def warehouse() -> FakeWarehouse:
    return FakeWarehouse()


@pytest.fixture
def pool(clock) -> FabricConnectionPool:
    return FabricConnectionPool(idle_seconds=300, max_age_seconds=2700, clock=clock)


@pytest.fixture
def fabric(warehouse, pool) -> FabricAdapter:
    return FabricAdapter(login_timeout=5, query_timeout=30, connect=warehouse, pool=pool)


def test_second_query_reuses_the_connection(fabric, warehouse, pool):
    fabric.run_query(CTX, secret(), "SELECT 1", 5)
    fabric.run_query(CTX, secret(), "SELECT 2", 5)
    assert len(warehouse.connects) == 1
    assert not warehouse.sessions[0].closed
    assert pool.idle_count() == 1


def test_a_new_token_gets_a_new_connection(fabric, warehouse):
    fabric.run_query(CTX, secret("token-1"), "SELECT 1", 5)
    fabric.run_query(CTX, secret("token-2"), "SELECT 1", 5)
    assert len(warehouse.connects) == 2


def test_another_connection_never_shares(fabric, warehouse):
    other = ConnectionContext(**{**CTX.__dict__, "id": "c2"})
    fabric.run_query(CTX, secret(), "SELECT 1", 5)
    fabric.run_query(other, secret(), "SELECT 1", 5)
    assert len(warehouse.connects) == 2


def test_a_failed_query_discards_its_connection(fabric, warehouse, pool):
    fabric.run_query(CTX, secret(), "SELECT 1", 5)
    warehouse.sessions[0].fail_next = RuntimeError("42S02", "Invalid object name 'x'.")
    with pytest.raises(AdapterError, match="Invalid object name"):
        fabric.run_query(CTX, secret(), "SELECT * FROM x", 5)
    assert warehouse.sessions[0].closed
    assert pool.idle_count() == 0
    fabric.run_query(CTX, secret(), "SELECT 1", 5)
    assert len(warehouse.connects) == 2


def test_a_dead_pooled_connection_is_replaced_transparently(fabric, warehouse):
    fabric.run_query(CTX, secret(), "SELECT 1", 5)
    warehouse.sessions[0].fail_next = RuntimeError("08S01", "Communication link failure")
    rows = fabric.run_query(CTX, secret(), "SELECT region, revenue FROM sales.x", 5)
    assert rows.rows  # answered on the fresh connection
    assert warehouse.sessions[0].closed
    assert len(warehouse.connects) == 2


def test_a_fresh_connection_failing_is_not_retried_forever(warehouse):
    adapter = FabricAdapter(login_timeout=5, query_timeout=30, connect=warehouse,
                            pool=FabricConnectionPool(idle_seconds=300, max_age_seconds=2700))
    warehouse.error = RuntimeError("08S01", "Communication link failure")
    with pytest.raises(AdapterError, match="Communication link failure"):
        adapter.run_query(CTX, secret(), "SELECT 1", 5)
    assert len(warehouse.connects) == 1


def test_idle_connections_are_closed(fabric, warehouse, pool, clock):
    fabric.run_query(CTX, secret(), "SELECT 1", 5)
    clock.time += 301
    fabric.run_query(CTX, secret(), "SELECT 1", 5)
    assert warehouse.sessions[0].closed
    assert len(warehouse.connects) == 2


def test_old_connections_are_not_kept(fabric, warehouse, pool, clock):
    fabric.run_query(CTX, secret(), "SELECT 1", 5)
    for _ in range(10):  # keep it busy so it never goes idle long
        clock.time += 280
        fabric.run_query(CTX, secret(), "SELECT 1", 5)
    # 2800 s after it opened -- past the 2700 s max age -- it must have been replaced.
    assert warehouse.sessions[0].closed
    assert len(warehouse.connects) == 2


class Closable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_simultaneous_queries_each_get_their_own(fabric, warehouse, pool):
    # A connection in use is out of the pool, so a second query opens its own.
    key = ("c1", "host", "db", "f")
    held = pool.take(key)
    assert held is None
    a, b, c = Closable(), Closable(), Closable()
    pool.give_back(key, a, pool.now())
    pool.give_back(key, b, pool.now())
    pool.give_back(key, c, pool.now())  # a third idle one for the same key: closed
    assert pool.idle_count() == 2
    assert c.closed and not a.closed and not b.closed


def test_total_idle_connections_are_capped(clock):
    pool = FabricConnectionPool(idle_seconds=300, max_age_seconds=2700,
                                max_idle_total=3, clock=clock)
    made = []
    for n in range(5):
        connection = Closable()
        made.append(connection)
        clock.time += 1
        pool.give_back((f"c{n}", "h", "d", "f"), connection, clock())
    assert pool.idle_count() == 3
    assert [m.closed for m in made] == [True, True, False, False, False]


def test_the_key_never_contains_the_token():
    fingerprint = token_fingerprint("eyJ.secret.token")
    assert "secret" not in fingerprint and len(fingerprint) == 24


def test_shutdown_closes_idle_connections(settings, warehouse):
    from iq_connect.main import create_app
    from iq_connect.settings import Settings

    values = {**settings.model_dump(), "entra_tenant": "11111111-2222-3333-4444-555555555555",
              "entra_client_id": "app", "entra_client_secret": "secret"}
    app = create_app(Settings(_env_file=None, **values))
    fabric = app.state.pipeline.registry.get("fabric")
    fabric._connect = warehouse
    with TestClient(app):
        fabric.run_query(CTX, secret(), "SELECT 1", 5)
        assert not warehouse.sessions[0].closed
    assert warehouse.sessions[0].closed
