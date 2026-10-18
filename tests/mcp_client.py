"""Talking MCP to /mcp the way Claude does, for the tests."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from iq_connect.connections import service
from iq_connect.db.models import Org, User
from oauth_client import FakeClaude

# The Host header must be one /mcp accepts (the public URL in the test settings).
BASE_URL = "http://localhost:8000"
DEMO_SCOPE = {"catalog": "demo", "schemas": ["sales"]}


def add_person(app, email: str, org_issuer: str = "https://login.example/acme") -> Any:
    from sqlalchemy import select

    with app.state.db.session() as session:
        org = session.scalars(select(Org).where(Org.idp_issuer == org_issuer)).one_or_none()
        org = org or Org(name="Acme", idp_issuer=org_issuer)
        user = User(org=org, idp_subject=f"oid-{email}", email=email)
        session.add(user)
        session.flush()
        return user.id


def add_connection(app, user_id, name: str = "Demo sales", **fields) -> Any:
    with app.state.db.session() as session:
        user = session.get(User, user_id)
        row = service.create(session, user, platform=fields.pop("platform", "demo"), name=name,
                             scope=fields.pop("scope", DEMO_SCOPE), **fields)
        return row.id


def token_for(app, user_id) -> str:
    claude = FakeClaude(app)
    claude.register()
    return claude.sign_in(user_id)["access_token"]


class Mcp:
    """An MCP client over plain HTTP. Use inside `with` so the app's lifespan runs."""

    def __init__(self, app, token: str | None) -> None:
        self.http = TestClient(app, base_url=BASE_URL)
        self.token = token
        self._next_id = 0

    def __enter__(self) -> Mcp:
        self.http.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.http.__exit__(*exc)

    def post(self, method: str, params: dict | None = None, headers: dict | None = None):
        self._next_id += 1
        all_headers = {"Accept": "application/json, text/event-stream"}
        if self.token:
            all_headers["Authorization"] = f"Bearer {self.token}"
        all_headers.update(headers or {})
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            body["params"] = params
        return self.http.post("/mcp", json=body, headers=all_headers)

    def tools(self) -> list[dict]:
        response = self.post("tools/list")
        assert response.status_code == 200, response.text
        return response.json()["result"]["tools"]

    def call(self, name: str, **arguments) -> tuple[str, bool]:
        response = self.post("tools/call", {"name": name, "arguments": arguments})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        text = "\n".join(part["text"] for part in result["content"] if part["type"] == "text")
        return text, result.get("isError", False)
