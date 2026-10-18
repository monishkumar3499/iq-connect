"""A stand-in for a person's browser on our pages, used by the sign-in tests."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient


class Browser:
    def __init__(self, app) -> None:
        # Keeps cookies between requests, like a real browser.
        self.http = TestClient(app, follow_redirects=False)

    def sign_in(self, email: str = "alice@example.com", name: str = "Alice",
                next_path: str = "/") -> str:
        """Dev sign-in from start to finish; returns where we ended up."""
        start = self.http.get("/auth/login", params={"next": next_path})
        assert start.status_code == 302, start.text
        form_url = start.headers["location"]
        assert form_url.startswith("/auth/dev-login?")
        nonce = parse_qs(urlsplit(form_url).query)["nonce"][0]
        assert self.http.get(form_url).status_code == 200
        posted = self.http.post("/auth/dev-login",
                                data={"email": email, "name": name, "nonce": nonce})
        assert posted.status_code == 303
        done = self.http.get(posted.headers["location"])
        assert done.status_code == 303, done.text
        return done.headers["location"]

    def csrf_on(self, path: str) -> str:
        page = self.http.get(path)
        assert page.status_code == 200, page.text
        return re.search(r"name='csrf' value='([^']+)'", page.text).group(1)
