from __future__ import annotations

import json
from dataclasses import replace

import pytest

from iq_connect.security.crypto import DecryptionError, LocalKeyring, SecretBox

CONTEXT = {"user_id": "alice", "provider": "snowflake"}


@pytest.fixture
def keyring(tmp_path) -> LocalKeyring:
    return LocalKeyring.load(tmp_path / "keys.json", create=True)


@pytest.fixture
def box(keyring) -> SecretBox:
    return SecretBox(keyring)


def test_round_trip(box):
    sealed = box.seal(b"refresh-token-123", CONTEXT)
    assert box.open(sealed, CONTEXT) == b"refresh-token-123"


def test_ciphertext_does_not_contain_the_secret(box):
    sealed = box.seal(b"refresh-token-123", CONTEXT)
    assert b"refresh-token-123" not in sealed.ciphertext
    assert b"refresh-token-123" not in sealed.wrapped_key


def test_same_secret_encrypts_differently_each_time(box):
    first, second = box.seal(b"same", CONTEXT), box.seal(b"same", CONTEXT)
    assert first.ciphertext != second.ciphertext
    assert first.wrapped_key != second.wrapped_key


def test_wrong_context_cannot_decrypt(box):
    sealed = box.seal(b"secret", CONTEXT)
    with pytest.raises(DecryptionError):
        box.open(sealed, {**CONTEXT, "user_id": "mallory"})


@pytest.mark.parametrize("field", ["ciphertext", "wrapped_key"])
def test_tampering_is_detected(box, field):
    sealed = box.seal(b"secret", CONTEXT)
    blob = bytearray(getattr(sealed, field))
    blob[-1] ^= 0x01
    with pytest.raises(DecryptionError):
        box.open(replace(sealed, **{field: bytes(blob)}), CONTEXT)


def test_garbage_is_rejected_cleanly(box):
    sealed = box.seal(b"secret", CONTEXT)
    with pytest.raises(DecryptionError):
        box.open(replace(sealed, ciphertext=b"nonsense"), CONTEXT)


def test_unknown_key_version_cannot_decrypt(box):
    sealed = box.seal(b"secret", CONTEXT)
    with pytest.raises(DecryptionError):
        box.open(replace(sealed, key_version="local-99"), CONTEXT)


def test_a_different_key_file_cannot_decrypt(box, tmp_path):
    sealed = box.seal(b"secret", CONTEXT)
    other = SecretBox(LocalKeyring.load(tmp_path / "other.json", create=True))
    with pytest.raises(DecryptionError):
        other.open(sealed, CONTEXT)


def test_rotation_keeps_old_secrets_readable(keyring, box, tmp_path):
    old = box.seal(b"old secret", CONTEXT)
    assert keyring.rotate() == "local-2"
    new = box.seal(b"new secret", CONTEXT)
    assert (old.key_version, new.key_version) == ("local-1", "local-2")

    reloaded = SecretBox(LocalKeyring.load(keyring.path, create=False))
    assert reloaded.open(old, CONTEXT) == b"old secret"
    assert reloaded.open(new, CONTEXT) == b"new secret"


def test_key_file_is_created_only_when_allowed(tmp_path):
    with pytest.raises(FileNotFoundError):
        LocalKeyring.load(tmp_path / "missing.json", create=False)
    assert not (tmp_path / "missing.json").exists()


def test_key_file_survives_a_reload(keyring, box):
    sealed = box.seal(b"secret", CONTEXT)
    reloaded = SecretBox(LocalKeyring.load(keyring.path, create=False))
    assert reloaded.open(sealed, CONTEXT) == b"secret"


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"keys": {}}),
        json.dumps({"current": "local-1", "keys": {"local-2": "AAAA"}}),
        json.dumps({"current": "local-1", "keys": {"local-1": "dG9vIHNob3J0"}}),
    ],
)
def test_broken_key_file_is_refused(tmp_path, content):
    path = tmp_path / "keys.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        LocalKeyring.load(path, create=False)


def test_readiness_check(box):
    box.check()
