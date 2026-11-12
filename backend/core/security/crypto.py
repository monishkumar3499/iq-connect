"""
Envelope encryption for stored secrets, and the local keys (encryption and signing) it
and the tokens use.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.core.configuration.settings import Settings
from backend.core.security.tokens import SigningKeys

log = logging.getLogger(__name__)

# First byte of every ciphertext. A future change of algorithm bumps it, and
# old rows remain readable because the reader knows which format it holds.
_FORMAT_V1 = b"\x01"
_NONCE_BYTES = 12


class DecryptionError(Exception):
    """A secret could not be decrypted. Deliberately says nothing about why."""


class KeyEncryptionKey(Protocol):
    """Wraps and unwraps data keys. Implementations: LocalKeyring, later Key Vault."""

    def wrap(self, data_key: bytes) -> tuple[str, bytes]:
        """Encrypt a data key with the current KEK. Returns (key_version, wrapped)."""

    def unwrap(self, key_version: str, wrapped: bytes) -> bytes:
        """Decrypt a data key wrapped by the given KEK version."""


@dataclass(frozen=True)
class Sealed:
    """What gets stored: maps onto Credential.ciphertext / wrapped_key / key_version."""

    ciphertext: bytes
    wrapped_key: bytes
    key_version: str


def _context_bytes(context: dict[str, str]) -> bytes:
    # One canonical encoding, so the same context always authenticates the same.
    return json.dumps(context, sort_keys=True, separators=(",", ":")).encode()


def _encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return _FORMAT_V1 + nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if not blob.startswith(_FORMAT_V1) or len(blob) < 1 + _NONCE_BYTES:
        raise DecryptionError("unreadable secret")
    nonce, body = blob[1 : 1 + _NONCE_BYTES], blob[1 + _NONCE_BYTES :]
    try:
        return AESGCM(key).decrypt(nonce, body, aad)
    except InvalidTag:
        raise DecryptionError("unreadable secret") from None


class SecretBox:
    """The one entry point the rest of the code uses to encrypt and decrypt."""

    def __init__(self, kek: KeyEncryptionKey) -> None:
        self._kek = kek

    def seal(self, plaintext: bytes, context: dict[str, str]) -> Sealed:
        data_key = AESGCM.generate_key(bit_length=256)
        ciphertext = _encrypt(data_key, plaintext, _context_bytes(context))
        key_version, wrapped = self._kek.wrap(data_key)
        return Sealed(ciphertext, wrapped, key_version)

    def open(self, sealed: Sealed, context: dict[str, str]) -> bytes:
        try:
            data_key = self._kek.unwrap(sealed.key_version, sealed.wrapped_key)
        except DecryptionError:
            raise
        except Exception:
            # A Key Vault outage must not look like tampering in the logs,
            # and must not leak details to the caller either.
            log.exception("Could not unwrap a data key (key version %s)", sealed.key_version)
            raise DecryptionError("unreadable secret") from None
        return _decrypt(data_key, sealed.ciphertext, _context_bytes(context))

    def check(self) -> None:
        """Readiness probe: the KEK can wrap and unwrap right now."""
        probe = self.seal(b"probe", {"purpose": "readiness"})
        if self.open(probe, {"purpose": "readiness"}) != b"probe":
            raise DecryptionError("key round trip failed")


class LocalKeyring:
    """
    KEK versions kept in a JSON file -- for development, and until Key Vault.

        {"current": "local-2", "keys": {"local-1": "<base64>", "local-2": "<base64>"}}

    Losing this file makes every stored credential unreadable (people would
    have to sign in to their platforms again). It must never be committed:
    `.local/` is in .gitignore.
    """

    def __init__(self, path: Path, current: str, keys: dict[str, bytes]) -> None:
        self.path = path
        self.current = current
        self._keys = keys

    @classmethod
    def load(cls, path: Path, *, create: bool) -> LocalKeyring:
        if not path.exists():
            if not create:
                raise FileNotFoundError(
                    f"Key file {path} does not exist. Outside APP_ENV=dev it is never created "
                    "automatically: a fresh key would silently orphan every stored credential."
                )
            keyring = cls(path, "local-1", {"local-1": AESGCM.generate_key(bit_length=256)})
            keyring._save()
            log.warning("Created a new local key file at %s -- keep it safe, never commit it", path)
            return keyring
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            keys = {version: base64.b64decode(value) for version, value in raw["keys"].items()}
            current = raw["current"]
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError(f"Key file {path} is not valid") from error
        if current not in keys or any(len(key) != 32 for key in keys.values()):
            raise ValueError(f"Key file {path} is not valid")
        return cls(path, current, keys)

    def all_keys(self) -> list[bytes]:
        """Every version, oldest first and the current one last."""
        ordered = sorted(self._keys, key=lambda v: (v == self.current, v))
        return [self._keys[version] for version in ordered]

    def key(self, version: str) -> bytes | None:
        """The raw key of one version (used for token signing keys), or None."""
        return self._keys.get(version)

    def rotate(self) -> str:
        """Add a new KEK version and make it current. Older versions stay for reading."""
        number = 1 + max(int(v.removeprefix("local-")) for v in self._keys)
        version = f"local-{number}"
        self._keys[version] = AESGCM.generate_key(bit_length=256)
        self.current = version
        self._save()
        return version

    def wrap(self, data_key: bytes) -> tuple[str, bytes]:
        aad = self.current.encode()
        return self.current, _encrypt(self._keys[self.current], data_key, aad)

    def unwrap(self, key_version: str, wrapped: bytes) -> bytes:
        key = self._keys.get(key_version)
        if key is None:
            raise DecryptionError("unreadable secret")
        return _decrypt(key, wrapped, key_version.encode())

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "current": self.current,
            "keys": {v: base64.b64encode(k).decode() for v, k in self._keys.items()},
        }
        # Write to a temporary file and swap it in, so a crash mid-write can
        # never leave a half-written key file behind.
        temporary = self.path.with_suffix(f".{secrets.token_hex(4)}.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def make_key_encryption_key(settings: Settings) -> KeyEncryptionKey:
    if settings.key_provider == "local":
        # Created on first run in dev only. In production a missing file is an
        # error: generating a new key there would orphan every stored secret.
        return LocalKeyring.load(Path(settings.local_key_file), create=settings.is_dev)
    # Azure Key Vault arrives with roadmap step 12; settings validation keeps
    # any other value from reaching this point.
    raise ValueError(f"Unknown KEY_PROVIDER {settings.key_provider!r}")


def make_signing_keys(settings: Settings) -> SigningKeys:
    if settings.key_provider == "local":
        # A new signing key in production would only log everyone out (tokens
        # are short lived), but every copy must share one key, so it is still
        # never invented silently outside dev.
        return LocalKeyring.load(Path(settings.local_signing_key_file), create=settings.is_dev)
    raise ValueError(f"Unknown KEY_PROVIDER {settings.key_provider!r}")
