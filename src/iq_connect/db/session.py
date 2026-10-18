"""
The database engine and sessions.

Which database is used is decided by one setting, DATABASE_URL. Everything
dialect-specific lives in `make_engine`; the rest of the code only ever sees
SQLAlchemy sessions, so switching databases never touches it.

Sessions are synchronous on purpose: platform drivers are synchronous too, and
both run in worker threads, never on the event loop.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str) -> Engine:
    parsed = make_url(url)

    if parsed.get_backend_name() == "sqlite":
        in_memory = parsed.database in (None, "", ":memory:")
        if not in_memory:
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        # Requests are served from several threads; SQLite's default is to
        # refuse a connection created on another thread. `timeout` makes a
        # writer wait for a lock instead of failing at once.
        engine = create_engine(parsed, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            # SQLite ignores foreign keys unless asked, per connection. Without
            # this, ON DELETE CASCADE silently does nothing and tests would
            # pass here but behave differently on PostgreSQL.
            cursor.execute("PRAGMA foreign_keys=ON")
            if not in_memory:
                # Readers no longer block the writer.
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine

    # Server databases: a bounded pool per copy of the service (see the
    # capacity section of the architecture), with dead connections detected
    # before use rather than on a user's request.
    return create_engine(
        parsed, pool_size=10, max_overflow=5, pool_pre_ping=True, pool_recycle=1800
    )


def safe_url(url: str) -> str:
    """The URL with any password masked, for logs."""
    return make_url(url).render_as_string(hide_password=True)


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = make_engine(url)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """One unit of work: commits if the block finishes, rolls back if it raises."""
        with self._sessions.begin() as session:
            yield session

    def check(self) -> None:
        """Readiness probe: can we reach the database at all?"""
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def dispose(self) -> None:
        self.engine.dispose()
