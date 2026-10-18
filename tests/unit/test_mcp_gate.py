"""/mcp lets nobody in without a valid access token for this server."""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from mcp_client import BASE_URL, Mcp, add_person, token_for

METADATA = 'resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp"'


def test_no_token_gets_the_sign_in_challenge(app):
    with Mcp(app, None) as mcp:
        response = mcp.post("tools/list")
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert METADATA in challenge
    assert "error=" not in challenge


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer    ", "token abc"])
def test_malformed_authorization_header(app, header):
    with Mcp(app, None) as mcp:
        response = mcp.post("tools/list", headers={"Authorization": header})
    assert response.status_code == 401


def test_garbage_token_is_invalid(app):
    with Mcp(app, "not-a-jwt") as mcp:
        response = mcp.post("tools/list")
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["www-authenticate"]


def forged(app, user_id, **overrides) -> str:
    """A token signed with our real key but with chosen claims."""
    keys = app.state.access_tokens._keys
    now = int(time.time())
    claims = {"iss": "http://localhost:8000", "sub": str(user_id), "aud": f"{BASE_URL}/mcp",
              "client_id": "x", "scope": "iq", "iat": now, "exp": now + 600, **overrides}
    return jwt.encode(claims, keys.key(keys.current), algorithm="HS256",
                      headers={"kid": keys.current})


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "https://other.example/mcp"},
        {"iss": "https://other.example"},
        {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200},
        {"sub": "not-a-uuid"},
    ],
)
def test_tokens_not_meant_for_us_are_refused(app, user_id, overrides):
    with Mcp(app, forged(app, user_id, **overrides)) as mcp:
        assert mcp.post("tools/list").status_code == 401


def test_token_without_our_scope_is_forbidden(app, user_id):
    with Mcp(app, forged(app, user_id, scope="something-else")) as mcp:
        response = mcp.post("tools/list")
    assert response.status_code == 403
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]


def test_valid_token_lists_the_tools(app):
    person = add_person(app, "alice@acme.com")
    with Mcp(app, token_for(app, person)) as mcp:
        names = {tool["name"] for tool in mcp.tools()}
    assert names == {"list_connections", "list_objects", "describe_table", "run_query",
                     "data_map", "profile_tables", "semantic_model_map",
                     "query_semantic_model"}


def test_tools_are_marked_read_only(app):
    person = add_person(app, "alice@acme.com")
    with Mcp(app, token_for(app, person)) as mcp:
        for tool in mcp.tools():
            assert tool["annotations"]["readOnlyHint"] is True
            assert tool["annotations"]["destructiveHint"] is False


def test_requests_for_another_host_are_refused(app):
    # DNS-rebinding protection: only our public host (and loopback in dev).
    person = add_person(app, "alice@acme.com")
    token = token_for(app, person)
    with TestClient(app, base_url="http://evil.example") as client:
        response = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 421
