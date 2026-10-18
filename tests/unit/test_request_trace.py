from __future__ import annotations

import logging

import pytest

TRACE_LOGGER = "iq_connect.request"


@pytest.fixture
def trace(caplog):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    return caplog


def trace_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == TRACE_LOGGER]


def test_logs_arrival_and_completion(client, trace):
    client.get("/nowhere")
    arrived, ended = trace_lines(trace)
    assert arrived == ">> GET /nowhere"
    assert ended.startswith("<< GET /nowhere -> 404 in ")


def test_never_logs_query_string(client, trace):
    client.get("/oauth/callback?code=SECRET-CODE&state=abc")
    assert all("SECRET-CODE" not in line for line in trace_lines(trace))


def test_http_client_libraries_cannot_log_urls(app):
    # They log full URLs (query strings with codes included) at INFO.
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)


def test_health_probes_are_not_traced(client, trace):
    client.get("/healthz")
    client.get("/readyz")
    assert trace_lines(trace) == []


def test_request_id_is_generated_and_returned(client):
    response = client.get("/healthz")
    assert len(response.headers["x-request-id"]) == 16


def test_safe_incoming_request_id_is_kept(client):
    response = client.get("/healthz", headers={"X-Request-ID": "lb-abc.123"})
    assert response.headers["x-request-id"] == "lb-abc.123"


def test_unsafe_incoming_request_id_is_replaced(client):
    response = client.get("/healthz", headers={"X-Request-ID": "x\tFAKE LOG LINE"})
    assert response.headers["x-request-id"] != "x\tFAKE LOG LINE"
    assert len(response.headers["x-request-id"]) == 16
