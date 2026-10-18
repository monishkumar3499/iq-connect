from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError

from iq_connect.db.models import AuditEvent, Credential, DataConnection, Org, User


def make_user(session, subject: str = "oid-1", org: Org | None = None) -> User:
    org = org or Org(name="Acme", idp_issuer=f"https://login.example/{subject}")
    user = User(org=org, idp_subject=subject, email=f"{subject}@acme.com")
    session.add(user)
    session.flush()
    return user


def make_connection(session, user: User, name: str = "Sales") -> DataConnection:
    connection = DataConnection(org_id=user.org_id, user=user, platform="snowflake", name=name)
    session.add(connection)
    session.flush()
    return connection


def make_credential(session, user: User) -> Credential:
    credential = Credential(
        user=user, provider="snowflake", account="acme-xy123",
        ciphertext=b"\x00secret", wrapped_key=b"\x01key", key_version="local-1",
    )
    session.add(credential)
    session.flush()
    return credential


def test_defaults_are_filled_in(db):
    with db.session() as session:
        connection = make_connection(session, make_user(session))
        assert connection.scope == {}
        assert connection.settings == {}
        assert connection.status == "active"
        assert connection.created_at.tzinfo is UTC


def test_connection_names_are_unique_per_person(db):
    with pytest.raises(IntegrityError):
        with db.session() as session:
            user = make_user(session)
            make_connection(session, user, "Sales")
            make_connection(session, user, "Sales")


def test_two_people_can_use_the_same_connection_name(db):
    with db.session() as session:
        org = Org(name="Acme", idp_issuer="https://login.example/acme")
        make_connection(session, make_user(session, "alice", org), "Sales")
        make_connection(session, make_user(session, "bob", org), "Sales")


def test_foreign_keys_are_enforced(db):
    # SQLite ignores foreign keys unless the connection turns them on; this
    # proves make_engine does, so SQLite behaves like a server database.
    import uuid

    with pytest.raises(IntegrityError):
        with db.session() as session:
            session.add(DataConnection(
                org_id=uuid.uuid4(), user_id=uuid.uuid4(), platform="fabric", name="Orphan",
            ))


def test_deleting_a_person_removes_their_connections_and_logins(db):
    with db.session() as session:
        user = make_user(session)
        make_connection(session, user)
        make_credential(session, user)
        user_id = user.id

    with db.session() as session:
        session.delete(session.get(User, user_id))

    with db.session() as session:
        assert session.scalars(select(DataConnection)).all() == []
        assert session.scalars(select(Credential)).all() == []


def test_unlinking_a_login_keeps_the_connection(db):
    with db.session() as session:
        user = make_user(session)
        connection = make_connection(session, user)
        connection.credential = make_credential(session, user)
        connection_id, credential_id = connection.id, connection.credential.id

    with db.session() as session:
        session.delete(session.get(Credential, credential_id))

    with db.session() as session:
        connection = session.get(DataConnection, connection_id)
        assert connection is not None
        assert connection.credential_id is None


def test_in_place_json_changes_are_saved(db):
    with db.session() as session:
        connection_id = make_connection(session, make_user(session)).id

    with db.session() as session:
        # Kept in a variable on purpose: see the warning on JSONDict.
        connection = session.get(DataConnection, connection_id)
        connection.scope["schemas"] = ["sales"]

    with db.session() as session:
        assert session.get(DataConnection, connection_id).scope == {"schemas": ["sales"]}


def test_datetimes_round_trip_as_utc(db):
    ist = timezone(timedelta(hours=5, minutes=30))
    with db.session() as session:
        user = make_user(session)
        user.last_login_at = datetime(2026, 10, 14, 17, 0, tzinfo=ist)
        user_id = user.id

    with db.session() as session:
        stored = session.get(User, user_id).last_login_at
        assert stored == datetime(2026, 10, 14, 11, 30, tzinfo=UTC)
        assert stored.tzinfo is UTC


def test_naive_datetimes_are_refused(db):
    with pytest.raises(StatementError):
        with db.session() as session:
            user = make_user(session)
            user.last_login_at = datetime(2026, 10, 14, 17, 0)
            session.flush()


def test_audit_rows_survive_without_their_user(db):
    import uuid

    with db.session() as session:
        session.add(AuditEvent(
            org_id=uuid.uuid4(), user_id=uuid.uuid4(), tool="run_query",
            duration_ms=12, outcome="ok",
        ))
    with db.session() as session:
        event = session.scalars(select(AuditEvent)).one()
        assert event.id == 1
