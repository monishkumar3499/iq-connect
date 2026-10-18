"""Alembic entry point: which database, which models."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from iq_connect.db.models import Base
from iq_connect.db.session import make_engine
from iq_connect.settings import get_settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# Tests pass their own URL; everyone else uses the app's DATABASE_URL, so the
# migrations always run against the same database the service will use.
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
