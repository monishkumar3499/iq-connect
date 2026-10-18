from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from iq_connect import ops
from iq_connect.main import create_app
from iq_connect.settings import Settings


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_checks_the_database(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "keys": "ok"}}


def test_readyz_reports_failure_without_leaking_details(app):
    async def ok() -> None:
        return None

    async def broken() -> None:
        raise RuntimeError("password=hunter2 host=db.internal")

    ops.add_readiness_check(app, "broken_dependency", broken)
    ops.add_readiness_check(app, "key_vault", ok)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {
        "database": "ok", "keys": "ok", "broken_dependency": "failed", "key_vault": "ok",
    }
    assert "hunter2" not in response.text


def test_readyz_times_out_a_hanging_check(app, monkeypatch):
    monkeypatch.setattr(ops, "CHECK_TIMEOUT_SECONDS", 0.05)

    async def hangs() -> None:
        await asyncio.sleep(5)

    ops.add_readiness_check(app, "slow", hangs)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["slow"] == "failed"


def test_docs_hidden_in_production(database_url, key_file):
    from iq_connect.security.crypto import LocalKeyring

    signing_file = str(Path(key_file).with_name("signing.json"))
    # Production never creates key files itself.
    LocalKeyring.load(Path(key_file), create=True)
    LocalKeyring.load(Path(signing_file), create=True)
    prod = Settings(
        _env_file=None, app_env="prod", public_base_url="https://iq.example.com",
        database_url=database_url, local_key_file=key_file, local_signing_key_file=signing_file,
        sso_provider="entra", entra_tenant="11111111-2222-3333-4444-555555555555",
        entra_client_id="app-id", entra_client_secret="secret",
    )
    client = TestClient(create_app(prod))
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
