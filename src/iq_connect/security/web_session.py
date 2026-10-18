"""
Browser sessions for our own pages (the Connect page, later the settings app).

The session is a signed cookie: tamper-proof, readable by any copy of the
service, nothing stored server-side. It holds only the user id, a CSRF token
and the time it was issued -- never a platform credential.

Cookie choices, each for a reason:
  HttpOnly          page scripts can't read it, so an injected script can't steal it
  SameSite=Lax      sent when Microsoft's sign-in redirects back to us (a top-level
                    GET), but not on cross-site form posts
  Secure + __Host-  over HTTPS: never sent in clear, and no subdomain can set or
                    overwrite it (the __Host- prefix makes browsers enforce that)
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer


@dataclass(frozen=True)
class BrowserSession:
    user_id: uuid.UUID
    csrf: str

    def csrf_matches(self, submitted: str | None) -> bool:
        return bool(submitted) and hmac.compare_digest(self.csrf, submitted)


class BrowserSessions:
    def __init__(self, keys: list[bytes], *, secure: bool, max_age_seconds: int) -> None:
        # itsdangerous signs with the last key and accepts any of them, so a
        # rotated signing key does not sign everyone out.
        self._session = URLSafeTimedSerializer(keys, salt="iq-browser-session")
        self._login = URLSafeTimedSerializer(keys, salt="iq-login-state")
        self._secure = secure
        self._max_age = max_age_seconds
        prefix = "__Host-" if secure else ""
        self.session_cookie = f"{prefix}iq_session"
        self.login_cookie = f"{prefix}iq_login"

    # --- the signed-in session ----------------------------------------------

    def start(self, response: Response, user_id: uuid.UUID) -> None:
        value = self._session.dumps({"uid": str(user_id), "csrf": secrets.token_urlsafe(24)})
        self._set(response, self.session_cookie, value, self._max_age)

    def read(self, request: Request) -> BrowserSession | None:
        data = self._load(self._session, request.cookies.get(self.session_cookie), self._max_age)
        if data is None:
            return None
        try:
            return BrowserSession(uuid.UUID(data["uid"]), data["csrf"])
        except (KeyError, ValueError, TypeError):
            return None

    def end(self, response: Response) -> None:
        response.delete_cookie(self.session_cookie, path="/", secure=self._secure,
                               httponly=True, samesite="lax")

    # --- state carried across the trip to the identity provider ---------------

    def keep_login_state(self, response: Response, state: dict[str, Any]) -> None:
        self._set(response, self.login_cookie, self._login.dumps(state), 600)

    def take_login_state(self, request: Request, response: Response) -> dict[str, Any] | None:
        response.delete_cookie(self.login_cookie, path="/", secure=self._secure,
                               httponly=True, samesite="lax")
        return self._load(self._login, request.cookies.get(self.login_cookie), 600)

    # --- helpers ---------------------------------------------------------------

    def _set(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(name, value, max_age=max_age, path="/", secure=self._secure,
                            httponly=True, samesite="lax")

    @staticmethod
    def _load(serializer: URLSafeTimedSerializer, value: str | None, max_age: int) -> Any:
        if not value:
            return None
        try:
            return serializer.loads(value, max_age=max_age)
        except BadSignature:  # includes SignatureExpired
            return None
