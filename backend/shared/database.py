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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, DateTime, Engine, create_engine, event, text
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.types import TypeDecorator


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


def migration_config(url: str) -> Any:
    """
    Alembic's settings, in code (no alembic.ini): the migrations folder, the file
    name pattern, and the database to run against.
    """
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.set_main_option("file_template",
                           "%%(year)d%%(month).2d%%(day).2d_%%(rev)s_%%(slug)s")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


# --- column types ----------------------------------------------------------------------
# Column types that behave the same on every database we may move to.
#
# Rule for the whole schema: use only these and SQLAlchemy's generic types
# (String, Text, Integer, Boolean, LargeBinary, Uuid, JSON). Nothing from a
# dialect package (no JSONB, ARRAY, NVARCHAR(MAX)...). That is what keeps a move
# from SQLite to PostgreSQL or SQL Server a settings change, not a rewrite.

class UTCDateTime(TypeDecorator[datetime]):
    """
    Stores UTC, always returns timezone-aware UTC.

    SQLite has no time zones and PostgreSQL's `timestamptz` converts to the
    session's zone, so "a datetime column" means something different on each.
    Storing naive UTC and re-attaching UTC on the way out gives one behaviour
    everywhere. Naive values are refused: their zone is a guess, and a guessed
    expiry time is a security bug.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime needs a timezone-aware datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


# A JSON object column whose in-place changes (`row.scope["schemas"] = [...]`)
# are noticed and saved. Plain JSON only saves when the whole value is replaced.
#
# Warning: keep the row in a variable while changing it. The session holds
# loaded rows only weakly, so `session.get(X, id).scope["k"] = v` on one line
# lets Python discard the row -- and the change -- before the commit. Only
# top-level keys are tracked; to change a nested value, assign the top-level
# key again.
JSONDict = MutableDict.as_mutable(JSON)


def utcnow() -> datetime:
    return datetime.now(UTC)
