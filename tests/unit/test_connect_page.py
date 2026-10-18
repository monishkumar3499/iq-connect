"""The Connect page: Claude's sign-in, carried by a real (test) browser."""

from __future__ import annotations

from browser import Browser
from oauth_client import CLAUDE_CALLBACK, FakeClaude, query


def start_from_claude(app) -> tuple[FakeClaude, str]:
    claude = FakeClaude(app)
    claude.register()
    response = claude.authorize()
    return claude, response.headers["location"]  # /connect?flow=...


def test_connecting_claude_end_to_end(app):
    claude, connect_url = start_from_claude(app)
    browser = Browser(app)

    # Not signed in yet: the Connect page sends us to sign in, then back.
    first = browser.http.get(connect_url)
    assert first.headers["location"].startswith("/auth/login?")
    assert browser.sign_in("alice@example.com", next_path=connect_url) == connect_url

    page = browser.http.get(connect_url)
    assert "alice@example.com" in page.text
    assert "Claude" in page.text
    csrf = browser.csrf_on(connect_url)

    flow = query(connect_url)["flow"]
    allowed = browser.http.post("/connect/approve", data={"flow": flow, "csrf": csrf})
    assert allowed.status_code == 303
    back = allowed.headers["location"]
    assert back.startswith(CLAUDE_CALLBACK)
    assert query(back)["state"] == "state-123"

    tokens = claude.exchange(query(back)["code"])
    assert tokens.status_code == 200
    claims = app.state.access_tokens.verify(
        tokens.json()["access_token"], audience=app.state.settings.mcp_url
    )
    page_user = browser.http.get("/").text
    assert "alice@example.com" in page_user
    assert claims["sub"]


def test_cancel_tells_claude(app):
    _, connect_url = start_from_claude(app)
    browser = Browser(app)
    browser.sign_in()
    csrf = browser.csrf_on(connect_url)
    denied = browser.http.post(
        "/connect/deny", data={"flow": query(connect_url)["flow"], "csrf": csrf}
    )
    assert query(denied.headers["location"])["error"] == "access_denied"


def test_approve_needs_the_page_csrf_token(app):
    _, connect_url = start_from_claude(app)
    browser = Browser(app)
    browser.sign_in()
    flow = query(connect_url)["flow"]
    assert browser.http.post("/connect/approve", data={"flow": flow}).status_code == 403
    response = browser.http.post("/connect/approve", data={"flow": flow, "csrf": "guess"})
    assert response.status_code == 403


def test_another_persons_csrf_token_does_not_work(app):
    _, connect_url = start_from_claude(app)
    alice, mallory = Browser(app), Browser(app)
    alice.sign_in("alice@example.com")
    mallory.sign_in("mallory@example.com")
    mallorys_token = mallory.csrf_on(connect_url)
    response = alice.http.post(
        "/connect/approve", data={"flow": query(connect_url)["flow"], "csrf": mallorys_token}
    )
    assert response.status_code == 403


def test_approve_while_signed_out_is_refused(app):
    _, connect_url = start_from_claude(app)
    response = Browser(app).http.post(
        "/connect/approve", data={"flow": query(connect_url)["flow"], "csrf": "x"}
    )
    assert response.status_code == 401


def test_unknown_or_used_flow_shows_a_message(app):
    _, connect_url = start_from_claude(app)
    browser = Browser(app)
    browser.sign_in()
    csrf = browser.csrf_on(connect_url)
    flow = query(connect_url)["flow"]
    assert browser.http.post("/connect/approve", data={"flow": flow, "csrf": csrf}).status_code \
        == 303
    again = browser.http.post("/connect/approve", data={"flow": flow, "csrf": csrf})
    assert again.status_code == 400
    assert browser.http.get("/connect?flow=made-up").status_code == 400
