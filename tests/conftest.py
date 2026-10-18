from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from iq_connect.db.models import Base
from iq_connect.db.session import Database
from iq_connect.main import create_app
from iq_connect.settings import Settings


@pytest.fixture
def database_url(tmp_path) -> str:
    # A real file, not :memory:. An in-memory SQLite database exists once per
    # connection, so a pooled engine would see a different empty database on
    # every connection.
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def key_file(tmp_path) -> str:
    return str(tmp_path / "keys.json")


@pytest.fixture
def settings(database_url: str, key_file: str, tmp_path) -> Settings:
    # _env_file=None: tests never read the developer's real .env, and every
    # file they create (database, key files) lives in a temporary folder.
    return Settings(
        _env_file=None,
        app_env="dev",
        public_base_url="http://localhost:8000",
        database_url=database_url,
        local_key_file=key_file,
        local_signing_key_file=str(tmp_path / "signing_keys.json"),
    )


@pytest.fixture
def app(settings: Settings):
    app = create_app(settings)
    Base.metadata.create_all(app.state.db.engine)
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def user_id(app):
    """A signed-up person in the app's database (step 5 will create these via SSO)."""
    from iq_connect.db.models import Org, User

    with app.state.db.session() as session:
        user = User(
            org=Org(name="Acme", idp_issuer="https://login.example/acme"),
            idp_subject="alice-oid",
            email="alice@acme.com",
        )
        session.add(user)
        session.flush()
        return user.id


@pytest.fixture
def db(database_url: str):
    """A database with every table created straight from the models."""
    database = Database(database_url)
    Base.metadata.create_all(database.engine)
    yield database
    database.dispose()
