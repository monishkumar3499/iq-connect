"""
The migrations must build exactly the schema the models describe, and undo
cleanly. If this fails after editing db/models.py, generate a migration:
    alembic revision --autogenerate -m "what changed"
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from iq_connect.db.models import Base
from iq_connect.db.session import make_engine

ROOT = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    # Keep alembic.ini's logging setup from replacing pytest's log capture.
    config.attributes["configure_logger"] = False
    return config


def test_migrations_match_the_models(database_url):
    command.upgrade(alembic_config(database_url), "head")
    engine = make_engine(database_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True, "render_as_batch": True}
            )
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert differences == [], f"models and migrations disagree: {differences}"


def test_migrations_downgrade_to_empty(database_url):
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    engine = make_engine(database_url)
    try:
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()
