"""The whole Claude sign-in, then every way it must refuse."""

from __future__ import annotations

from datetime import timedelta

import pytest

from iq_connect.oauth import common
from iq_connect.oauth.authorize import FlowNotFound, complete_flow, deny_flow
from iq_connect.security.tokens import InvalidToken
from oauth_client import CIMD_URL, CLAUDE_CALLBACK, MCP_URL, FakeClaude, query


@pytest.fixture
def claude(app) -> FakeClaude:
    claude = FakeClaude(app)
    assert claude.register().status_code == 201
    return claude


@pytest.fixture
def clock(monkeypatch):
    """Move the OAuth server's clock forward: clock(minutes=5)."""
    start = common.now()

    def advance(**delta):
        moved = start + timedelta(**delta)
        monkeypatch.setattr(common, "now", lambda: moved)

    return advance


# --- the happy paths ----------------------------------------------------------


def test_full_sign_in(app, claude, user_id):
    callback = claude.finish_as(claude.start(), user_id)
    assert callback["state"] == "state-123"
    assert callback["iss"] == "http://localhost:8000"

    response = claude.exchange(callback["code"])
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    tokens = response.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    assert tokens["scope"] == "iq"

    claims = app.state.access_tokens.verify(tokens["access_token"], audience=MCP_URL)
    assert claims["sub"] == str(user_id)
    assert claims["client_id"] == claude.client_id


def test_sign_in_with_cimd(app, user_id):
    app.state.cimd_fetch = lambda url: {
        "client_id": CIMD_URL, "client_name": "Claude", "redirect_uris": [CLAUDE_CALLBACK],
    }
    assert FakeClaude(app, client_id=CIMD_URL).sign_in(user_id)["access_token"]


def test_claude_code_loopback_on_any_port(app, user_id):
    claude = FakeClaude(app, redirect_uri="http://127.0.0.1:33418/callback")
    claude.register()
    claude.redirect_uri = "http://127.0.0.1:61001/callback"  # a different free port today
    assert claude.sign_in(user_id)["refresh_token"]


def test_scope_and_resource_may_be_omitted(claude, user_id):
    response = claude.authorize(scope=None, resource=None)
    assert response.status_code == 302
    flow = query(response.headers["location"])["flow"]
    callback = claude.finish_as(flow, user_id)
    assert claude.exchange(callback["code"], resource=None).json()["scope"] == "iq"


def test_unknown_scopes_are_ignored(claude, user_id):
    flow = query(claude.authorize(scope="iq offline_access admin").headers["location"])["flow"]
    callback = claude.finish_as(flow, user_id)
    assert claude.exchange(callback["code"]).json()["scope"] == "iq"


def test_cancelled_sign_in_tells_claude(app, claude):
    flow = claude.start()
    with app.state.db.session() as session:
        url = deny_flow(session, flow, app.state.settings.issuer)
    assert query(url)["error"] == "access_denied"
    assert query(url)["state"] == "state-123"


# --- /oauth/authorize refusals ----------------------------------------------


def test_unknown_client_gets_an_error_page_not_a_redirect(app):
    response = FakeClaude(app, client_id="dcr_made_up").authorize()
    assert response.status_code == 400
    assert "location" not in response.headers


def test_unregistered_redirect_gets_an_error_page(claude):
    response = claude.authorize(redirect_uri="https://claude.ai/somewhere-else")
    assert response.status_code == 400
    assert "location" not in response.headers


def test_repeated_parameter_is_refused(claude):
    response = claude.http.get(
        f"/oauth/authorize?client_id={claude.client_id}&client_id=other&response_type=code"
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge": None}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": "too-short"}, "invalid_request"),
        ({"resource": "https://other.example/mcp"}, "invalid_target"),
    ],
)
def test_bad_requests_are_sent_back_to_claude(claude, overrides, error):
    response = claude.authorize(**overrides)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(CLAUDE_CALLBACK)
    assert query(location)["error"] == error
    assert query(location)["state"] == "state-123"


def test_expired_sign_in_cannot_be_completed(app, claude, user_id, clock):
    flow = claude.start()
    clock(minutes=31)
    with pytest.raises(FlowNotFound), app.state.db.session() as session:
        complete_flow(session, flow, user_id, app.state.settings.issuer)


def test_sign_in_can_only_be_completed_once(claude, user_id):
    flow = claude.start()
    claude.finish_as(flow, user_id)
    with pytest.raises(FlowNotFound):
        claude.finish_as(flow, user_id)


# --- code exchange refusals -------------------------------------------------


def code_for(claude, user_id) -> str:
    return claude.finish_as(claude.start(), user_id)["code"]


def test_wrong_verifier_is_refused(claude, user_id):
    response = claude.exchange(code_for(claude, user_id), code_verifier="x" * 50)
    assert response.json()["error"] == "invalid_grant"


def test_wrong_redirect_uri_is_refused(claude, user_id):
    response = claude.exchange(code_for(claude, user_id), redirect_uri=CLAUDE_CALLBACK + "x")
    assert response.json()["error"] == "invalid_grant"


def test_wrong_client_is_refused(app, claude, user_id):
    other = FakeClaude(app)
    other.register()
    response = claude.exchange(code_for(claude, user_id), client_id=other.client_id)
    assert response.json()["error"] == "invalid_grant"


def test_wrong_resource_is_refused(claude, user_id):
    response = claude.exchange(code_for(claude, user_id), resource="https://other.example/mcp")
    assert response.json()["error"] == "invalid_target"


def test_expired_code_is_refused(claude, user_id, clock):
    code = code_for(claude, user_id)
    clock(minutes=3)
    assert claude.exchange(code).json()["error"] == "invalid_grant"


def test_reused_code_revokes_what_it_issued(claude, user_id):
    code = code_for(claude, user_id)
    first = claude.exchange(code).json()
    second = claude.exchange(code)
    assert second.json()["error"] == "invalid_grant"
    # The refresh token from the first exchange no longer works either.
    assert claude.refresh(first["refresh_token"]).json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    ("data", "headers"),
    [
        ('{"grant_type": "authorization_code"}', {"content-type": "application/json"}),
        ("grant_type=password&username=a", {"content-type": "application/x-www-form-urlencoded"}),
        ("grant_type=authorization_code", {"content-type": "application/x-www-form-urlencoded"}),
        (
            "grant_type=refresh_token&grant_type=authorization_code",
            {"content-type": "application/x-www-form-urlencoded"},
        ),
    ],
)
def test_malformed_token_requests(client, data, headers):
    response = client.post("/oauth/token", content=data, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"] in {
        "invalid_request", "unsupported_grant_type",
    }


# --- refresh tokens ---------------------------------------------------------


def test_refresh_rotates_the_token(app, claude, user_id):
    first = claude.sign_in(user_id)
    second = claude.refresh(first["refresh_token"]).json()
    assert second["refresh_token"] != first["refresh_token"]
    app.state.access_tokens.verify(second["access_token"], audience=MCP_URL)
    third = claude.refresh(second["refresh_token"])
    assert third.status_code == 200


def test_old_refresh_token_reused_later_revokes_everything(claude, user_id, clock):
    first = claude.sign_in(user_id)
    second = claude.refresh(first["refresh_token"]).json()
    clock(minutes=5)
    assert claude.refresh(first["refresh_token"]).json()["error"] == "invalid_grant"
    # The thief's replay also ended the legitimate chain: sign in again.
    assert claude.refresh(second["refresh_token"]).json()["error"] == "invalid_grant"


def test_quick_retry_after_a_lost_answer_still_works(claude, user_id):
    first = claude.sign_in(user_id)
    lost = claude.refresh(first["refresh_token"]).json()  # answer never reached Claude
    retried = claude.refresh(first["refresh_token"])
    assert retried.status_code == 200
    # Only the newest token stays alive.
    assert claude.refresh(lost["refresh_token"]).json()["error"] == "invalid_grant"
    assert claude.refresh(retried.json()["refresh_token"]).status_code == 200


def test_expired_refresh_token_is_refused(claude, user_id, clock):
    tokens = claude.sign_in(user_id)
    clock(days=31)
    assert claude.refresh(tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_refresh_cannot_widen_scope(claude, user_id):
    tokens = claude.sign_in(user_id)
    response = claude.refresh(tokens["refresh_token"], scope="iq admin")
    assert response.json()["error"] == "invalid_scope"


def test_refresh_token_belongs_to_its_client(app, claude, user_id):
    tokens = claude.sign_in(user_id)
    other = FakeClaude(app)
    other.register()
    assert other.refresh(tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_revocation_ends_the_connection(claude, user_id):
    tokens = claude.sign_in(user_id)
    response = claude.http.post(
        "/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": claude.client_id}
    )
    assert response.status_code == 200
    assert claude.refresh(tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_revoking_an_unknown_token_still_answers_200(claude):
    response = claude.http.post(
        "/oauth/revoke", data={"token": "nope", "client_id": claude.client_id}
    )
    assert response.status_code == 200


# --- access tokens ----------------------------------------------------------


def test_access_token_is_only_for_our_mcp(app, claude, user_id):
    token = claude.sign_in(user_id)["access_token"]
    with pytest.raises(InvalidToken):
        app.state.access_tokens.verify(token, audience="https://other.example/mcp")


def test_tampered_access_token_is_refused(app, claude, user_id):
    token = claude.sign_in(user_id)["access_token"]
    header, payload, signature = token.split(".")
    changed = "AA" if signature[-2:] != "AA" else "BB"
    forged = ".".join([header, payload, signature[:-2] + changed])
    with pytest.raises(InvalidToken):
        app.state.access_tokens.verify(forged, audience=MCP_URL)


def test_unsigned_access_token_is_refused(app, user_id):
    import jwt

    forged = jwt.encode(
        {"iss": "http://localhost:8000", "sub": str(user_id), "aud": MCP_URL,
         "iat": 0, "exp": 9999999999},
        key=None, algorithm="none",
    )
    with pytest.raises(InvalidToken):
        app.state.access_tokens.verify(forged, audience=MCP_URL)


def test_codes_and_refresh_tokens_are_stored_hashed(app, claude, user_id):
    from sqlalchemy import select

    from iq_connect.db.models import OAuthCode, OAuthRefreshToken

    code = code_for(claude, user_id)
    tokens = claude.exchange(code).json()
    with app.state.db.session() as session:
        stored = [row.code_hash for row in session.scalars(select(OAuthCode))]
        stored += [row.token_hash for row in session.scalars(select(OAuthRefreshToken))]
    assert code not in stored and tokens["refresh_token"] not in stored
