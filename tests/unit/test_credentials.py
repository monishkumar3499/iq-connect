from __future__ import annotations

import pytest

from iq_connect.credentials.service import read_secret, save_secret
from iq_connect.db.models import Credential, Org, User
from iq_connect.security.crypto import DecryptionError, LocalKeyring, SecretBox


@pytest.fixture
def box(tmp_path) -> SecretBox:
    return SecretBox(LocalKeyring.load(tmp_path / "keys.json", create=True))


def people(session) -> tuple[User, User]:
    org = Org(name="Acme", idp_issuer="https://login.example/acme")
    alice = User(org=org, idp_subject="alice", email="alice@acme.com")
    mallory = User(org=org, idp_subject="mallory", email="mallory@acme.com")
    session.add_all([alice, mallory])
    session.flush()
    return alice, mallory


def save(session, box, user, secret):
    return save_secret(
        session, box, user=user, provider="snowflake", account="acme-xy123", secret=secret
    )


def test_save_and_read(db, box):
    with db.session() as session:
        alice, _ = people(session)
        credential_id = save(session, box, alice, {"private_key": "-----BEGIN..."}).id

    with db.session() as session:
        credential = session.get(Credential, credential_id)
        assert read_secret(box, credential) == {"private_key": "-----BEGIN..."}
        assert b"BEGIN" not in credential.ciphertext


def test_saving_again_replaces_and_bumps_the_version(db, box):
    with db.session() as session:
        alice, _ = people(session)
        first = save(session, box, alice, {"refresh_token": "one"})
        second = save(session, box, alice, {"refresh_token": "two"})
        assert first.id == second.id
        assert second.version == 2
        assert read_secret(box, second) == {"refresh_token": "two"}


def test_a_login_moved_to_another_person_cannot_be_read(db, box):
    # Someone with database access re-points Alice's row at their own account.
    with db.session() as session:
        alice, mallory = people(session)
        credential = save(session, box, alice, {"refresh_token": "alice's"})
        credential.user_id = mallory.id
        session.flush()
        with pytest.raises(DecryptionError):
            read_secret(box, credential)


def test_ciphertexts_cannot_be_swapped_between_rows(db, box):
    with db.session() as session:
        alice, mallory = people(session)
        alices = save(session, box, alice, {"refresh_token": "alice's"})
        mallorys = save(session, box, mallory, {"refresh_token": "mallory's"})
        mallorys.ciphertext, mallorys.wrapped_key = alices.ciphertext, alices.wrapped_key
        with pytest.raises(DecryptionError):
            read_secret(box, mallorys)
