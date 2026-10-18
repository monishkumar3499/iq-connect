from __future__ import annotations

import pytest

from iq_connect.oauth.clients import redirect_allowed, redirect_matches
from oauth_client import CIMD_URL, CLAUDE_CALLBACK, FakeClaude

HOSTS = frozenset({"claude.ai", "claude.com"})


# --- redirect address rules -------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        CLAUDE_CALLBACK,
        "https://claude.com/api/mcp/auth_callback",
        "http://127.0.0.1:53682/callback",
        "http://localhost/callback",
        "http://[::1]:9000/cb",
    ],
)
def test_allowed_redirects(uri):
    assert redirect_allowed(uri, HOSTS)


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/callback",
        "https://claude.ai.evil.example/callback",
        "http://claude.ai/api/mcp/auth_callback",  # not https
        "https://claude.ai/cb#fragment",
        "https://user:pass@claude.ai/cb",
        "javascript:alert(1)",
        "",
        "https://claude.ai/" + "a" * 3000,
    ],
)
def test_refused_redirects(uri):
    assert not redirect_allowed(uri, HOSTS)


def test_loopback_port_is_ignored_but_path_is_not():
    registered = ("http://127.0.0.1:1234/callback",)
    assert redirect_matches("http://127.0.0.1:55555/callback", registered)
    assert not redirect_matches("http://127.0.0.1:55555/other", registered)
    assert not redirect_matches("http://localhost:55555/callback", registered)


def test_non_loopback_must_match_exactly():
    assert not redirect_matches(CLAUDE_CALLBACK + "?x=1", (CLAUDE_CALLBACK,))


# --- dynamic client registration --------------------------------------------


def test_dcr_registers_a_public_client(app):
    response = FakeClaude(app).register(token_endpoint_auth_method="client_secret_post")
    assert response.status_code == 201
    body = response.json()
    assert body["client_id"].startswith("dcr_")
    assert body["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in body
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "body",
    [
        {"redirect_uris": ["https://evil.example/cb"]},
        {"redirect_uris": []},
        {"redirect_uris": [CLAUDE_CALLBACK], "grant_types": ["client_credentials"]},
        {"redirect_uris": [CLAUDE_CALLBACK], "response_types": ["token"]},
        {"client_name": "no redirects"},
        ["not", "an", "object"],
    ],
)
def test_dcr_refuses_bad_registrations(client, body):
    response = client.post("/oauth/register", json=body)
    assert response.status_code == 400
    assert response.json()["error"] in {"invalid_redirect_uri", "invalid_client_metadata"}


def test_dcr_refuses_non_json(client):
    response = client.post("/oauth/register", content=b"redirect_uris=x")
    assert response.status_code == 400


# --- client id metadata documents -------------------------------------------


class FakeDocuments:
    """Serves CIMD documents and records every URL it was asked for."""

    def __init__(self, documents: dict[str, dict]):
        self.documents = documents
        self.requested: list[str] = []

    def __call__(self, url: str) -> dict:
        self.requested.append(url)
        if url not in self.documents:
            raise RuntimeError("404")
        return self.documents[url]


def claude_document(**overrides) -> dict:
    return {"client_id": CIMD_URL, "client_name": "Claude",
            "redirect_uris": [CLAUDE_CALLBACK], **overrides}


def test_cimd_client_can_start_a_sign_in(app):
    app.state.cimd_fetch = FakeDocuments({CIMD_URL: claude_document()})
    assert FakeClaude(app, client_id=CIMD_URL).start()


def test_cimd_document_is_cached(app):
    documents = FakeDocuments({CIMD_URL: claude_document()})
    app.state.cimd_fetch = documents
    claude = FakeClaude(app, client_id=CIMD_URL)
    claude.start()
    claude.start()
    assert documents.requested == [CIMD_URL]


def test_cached_document_is_used_when_refresh_fails(app, monkeypatch):
    from datetime import timedelta

    from iq_connect.oauth import common

    app.state.cimd_fetch = FakeDocuments({CIMD_URL: claude_document()})
    claude = FakeClaude(app, client_id=CIMD_URL)
    claude.start()
    later = common.now() + timedelta(hours=2)
    monkeypatch.setattr(common, "now", lambda: later)
    app.state.cimd_fetch = FakeDocuments({})  # claude.ai unreachable
    assert claude.start()


@pytest.mark.parametrize(
    "client_id",
    [
        "https://evil.example/client.json",
        "https://claude.ai.evil.example/client.json",
        "https://claude.ai:8443/client.json",
        "https://claude.ai/",
        "http://claude.ai/client.json",
        "https://user@claude.ai/client.json",
    ],
)
def test_cimd_is_never_fetched_from_other_places(app, client_id):
    documents = FakeDocuments({client_id: claude_document(client_id=client_id)})
    app.state.cimd_fetch = documents
    response = FakeClaude(app, client_id=client_id).authorize()
    assert response.status_code == 400
    assert documents.requested == []


@pytest.mark.parametrize(
    "document",
    [
        claude_document(client_id="https://claude.ai/someone-else"),
        claude_document(redirect_uris=["https://evil.example/cb"]),
        claude_document(redirect_uris=[]),
        claude_document(redirect_uris="https://claude.ai/api/mcp/auth_callback"),
    ],
)
def test_invalid_cimd_documents_are_refused(app, document):
    app.state.cimd_fetch = FakeDocuments({CIMD_URL: document})
    assert FakeClaude(app, client_id=CIMD_URL).authorize().status_code == 400


def test_cimd_switched_off_refuses_url_clients(app):
    app.state.settings = app.state.settings.model_copy(update={"oauth_cimd_enabled": False})
    app.state.cimd_fetch = FakeDocuments({CIMD_URL: claude_document()})
    assert FakeClaude(app, client_id=CIMD_URL).authorize().status_code == 400
