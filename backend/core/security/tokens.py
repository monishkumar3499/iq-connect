"""
The access tokens we give Claude.

They are signed JWTs, so any copy of the service can check one without a
database lookup -- every /mcp call carries one. They are deliberately short
lived (an hour by default): a signed token cannot be recalled once issued, so
revoking someone's access takes effect when their current token expires and
the refresh (which *is* checked against the database) is refused.

Signed with HMAC-SHA256 using keys from their own key file, separate from the
encryption keys: a leak of one must not expose the other. The key version goes
in the token header (`kid`), so rotating the signing key leaves tokens signed
with the previous version valid until they expire.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Any, Protocol

import jwt


class InvalidToken(Exception):
    """The token is not one we issued for this audience, or it has expired."""


class SigningKeys(Protocol):
    current: str

    def key(self, version: str) -> bytes | None: ...

    def all_keys(self) -> list[bytes]: ...


class AccessTokens:
    def __init__(self, keys: SigningKeys, *, issuer: str, lifetime: timedelta) -> None:
        self._keys = keys
        self.issuer = issuer
        self.lifetime = lifetime

    def issue(self, *, user_id: str, client_id: str, scope: str, audience: str) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "sub": user_id,
            "aud": audience,
            "client_id": client_id,
            "scope": scope,
            "iat": now,
            "exp": now + int(self.lifetime.total_seconds()),
            "jti": uuid.uuid4().hex,
        }
        key = self._keys.key(self._keys.current)
        assert key is not None
        return jwt.encode(claims, key, algorithm="HS256", headers={"kid": self._keys.current})

    def verify(self, token: str, *, audience: str) -> dict[str, Any]:
        try:
            version = jwt.get_unverified_header(token).get("kid", "")
            key = self._keys.key(version)
            if key is None:
                raise InvalidToken("unknown signing key")
            return jwt.decode(
                token,
                key,
                # Pinned: the algorithm is never taken from the token itself,
                # which is how "alg: none" style forgeries work.
                algorithms=["HS256"],
                audience=audience,
                issuer=self.issuer,
                leeway=30,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError as error:
            raise InvalidToken(str(error)) from None
