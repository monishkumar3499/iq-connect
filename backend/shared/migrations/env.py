"""Alembic entry point: which database, which models."""

from __future__ import annotations

from alembic import context

from backend.core.configuration.settings import get_settings
from backend.shared.database import make_engine
from backend.shared.models import Base

config = context.config
# `python -m backend.main migrate` passes the app's DATABASE_URL (db/session.py::
# migration_config), so the migrations always run against the service's database.
url = config.get_main_option("sqlalchemy.url") or get_settings().database_url

# render_as_batch: SQLite cannot ALTER most things in place, so Alembic copies
# the table instead. On other databases it emits ordinary ALTERs. Keeping it
# on everywhere means one set of migration files works on every database.
COMMON = {"target_metadata": Base.metadata, "render_as_batch": True, "compare_type": True}


def run_offline() -> None:
    context.configure(url=url, literal_binds=True, **COMMON)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, **COMMON)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
