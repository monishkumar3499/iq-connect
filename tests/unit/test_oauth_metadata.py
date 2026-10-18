from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from iq_connect.main import create_app
from iq_connect.settings import Settings


@pytest.mark.parametrize(
    "path", ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"]
)
def test_protected_resource_document(client, path):
    document = client.get(path).json()
    assert document["resource"] == "http://localhost:8000/mcp"
    assert document["authorization_servers"] == ["http://localhost:8000"]
    assert document["scopes_supported"] == ["iq"]


def test_authorization_server_document(client):
    document = client.get("/.well-known/oauth-authorization-server").json()
    assert document["issuer"] == "http://localhost:8000"
    assert document["authorization_endpoint"] == "http://localhost:8000/oauth/authorize"
    assert document["token_endpoint"] == "http://localhost:8000/oauth/token"
    assert document["registration_endpoint"] == "http://localhost:8000/oauth/register"
    assert document["code_challenge_methods_supported"] == ["S256"]
    assert "none" in document["token_endpoint_auth_methods_supported"]
    assert document["client_id_metadata_document_supported"] is True


def test_no_url_has_a_trailing_slash(client):
    # Claude compares these character for character with what it fetched.
    resource = client.get("/.well-known/oauth-protected-resource").json()
    server = client.get("/.well-known/oauth-authorization-server").json()
    urls = [resource["resource"], *resource["authorization_servers"], server["issuer"]]
    assert all(not url.endswith("/") for url in urls)


def test_trailing_slash_in_settings_does_not_leak_into_documents(settings):
    values = {**settings.model_dump(), "public_base_url": "http://localhost:8000/"}
    app = create_app(Settings(_env_file=None, **values))
    issuer = TestClient(app).get("/.well-known/oauth-authorization-server").json()["issuer"]
    assert issuer == "http://localhost:8000"


def test_cimd_can_be_switched_off(settings):
    app = create_app(settings.model_copy(update={"oauth_cimd_enabled": False}))
    document = TestClient(app).get("/.well-known/oauth-authorization-server").json()
    assert "client_id_metadata_document_supported" not in document
