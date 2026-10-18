"""A stand-in for Claude's side of the sign-in, used by the OAuth tests."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from iq_connect.oauth.authorize import complete_flow

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CIMD_URL = "https://claude.ai/oauth/mcp-oauth-client-metadata"
MCP_URL = "http://localhost:8000/mcp"


def query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


class FakeClaude:
    def __init__(self, app, redirect_uri: str = CLAUDE_CALLBACK, client_id: str = "") -> None:
        self.app = app
        self.http = TestClient(app, follow_redirects=False)
        self.redirect_uri = redirect_uri
        self.client_id = client_id
        self.verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(self.verifier.encode()).digest()
        self.challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def register(self, **extra):
        body = {"redirect_uris": [self.redirect_uri], "client_name": "Claude", **extra}
        response = self.http.post("/oauth/register", json=body)
        if response.status_code == 201:
            self.client_id = response.json()["client_id"]
        return response

    def authorize(self, **overrides):
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "state": "state-123",
            "scope": "iq",
            "resource": MCP_URL,
            **overrides,
        }
        params = {key: value for key, value in params.items() if value is not None}
        return self.http.get("/oauth/authorize", params=params)

    def start(self) -> str:
        """Begin a sign-in; returns the flow id our /connect page would receive."""
        response = self.authorize()
        assert response.status_code == 302, response.text
        location = response.headers["location"]
        assert location.startswith("/connect?")
        return query(location)["flow"]

    def finish_as(self, flow_id: str, user_id) -> dict[str, str]:
        """What step 5's /connect page does once the person has signed in."""
        settings = self.app.state.settings
        with self.app.state.db.session() as session:
            url = complete_flow(session, flow_id, user_id, settings.issuer)
        assert url.startswith(self.redirect_uri.split("?")[0])
        return query(url)

    def exchange(self, code: str, **overrides):
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_verifier": self.verifier,
            "resource": MCP_URL,
            **overrides,
        }
        form = {key: value for key, value in form.items() if value is not None}
        return self.http.post("/oauth/token", data=form)

    def refresh(self, refresh_token: str, **overrides):
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            **overrides,
        }
        return self.http.post("/oauth/token", data=form)

    def sign_in(self, user_id) -> dict:
        """The whole happy path; returns the token response."""
        callback = self.finish_as(self.start(), user_id)
        response = self.exchange(callback["code"])
        assert response.status_code == 200, response.text
        return response.json()
