"""Chooses where keys come from, based on settings. The only place that knows the options."""

from __future__ import annotations

from pathlib import Path

from ..settings import Settings
from .crypto import KeyEncryptionKey, LocalKeyring
from .tokens import SigningKeys


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
