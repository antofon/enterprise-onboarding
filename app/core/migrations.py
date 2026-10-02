"""schema migrations, applied by the app itself.

Alembic owns the schema. The api brings the database to the newest revision at startup so a
reviewer still gets one `docker compose up`; in a production deployment the same call runs as a
release step (`enterprise-onboarding migrate`) before the new version takes traffic.

Two details make that safe to run on every start:

  one migrator at a time   a postgres advisory lock is held for the whole upgrade, so two
                           processes starting together queue instead of racing each other.
  databases from before    a database whose tables were made by `create_all`, before migrations
                           existed, holds exactly the baseline's tables. It is stamped as the
                           baseline and then upgraded like any other.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, text
from sqlalchemy.engine import Engine

from app.core.db import ONBOARDING_SCHEMA, TARGET_SCHEMA
from app.core.logging import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
BASELINE_REVISION = "0001"
# any constant works, as long as every process that migrates uses the same one
MIGRATION_LOCK_KEY = 7_221_001


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(connection: Connection) -> str | None:
    context = MigrationContext.configure(
        connection, opts={"version_table_schema": ONBOARDING_SCHEMA}
    )
    return context.get_current_revision()


def upgrade(engine: Engine, revision: str = "head") -> str | None:
    """bring the database to `revision` and return where it ended up."""
    with engine.begin() as connection:
        connection.execute(text("select pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        for schema in (ONBOARDING_SCHEMA, TARGET_SCHEMA):
            connection.execute(text(f'create schema if not exists "{schema}"'))
        inspector = inspect(connection)
        versioned = inspector.has_table("alembic_version", schema=ONBOARDING_SCHEMA)
        from_before = inspector.has_table("projects", schema=ONBOARDING_SCHEMA)
        config = alembic_config(connection)
        before = current_revision(connection) if versioned else None
        if from_before and not versioned:
            command.stamp(config, BASELINE_REVISION)
            before = BASELINE_REVISION
            log.info(
                "database_stamped",
                revision=BASELINE_REVISION,
                reason="tables created before migrations existed",
            )
        command.upgrade(config, revision)
        after = current_revision(connection)
    if after != before:
        log.info("database_migrated", from_revision=before, to_revision=after)
    return after


__all__ = ["BASELINE_REVISION", "alembic_config", "current_revision", "head_revision", "upgrade"]
