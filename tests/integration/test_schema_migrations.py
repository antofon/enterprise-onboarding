"""alembic owns the schema. These run the real migrations against throwaway databases on the
compose postgres: from empty, from a database create_all built before migrations existed, twice
in a row, and down and up again. "The same schema" is checked the way alembic's autogenerate
would check it, so a model change without a migration fails here."""

from __future__ import annotations

import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

import app.models  # noqa: F401
from app.core.config import get_settings
from app.core.db import ONBOARDING_SCHEMA, TARGET_SCHEMA, Base, connect_args
from app.core.migrations import (
    BASELINE_REVISION,
    alembic_config,
    current_revision,
    head_revision,
    upgrade,
)

pytestmark = pytest.mark.integration


def _admin() -> Engine:
    url = make_url(get_settings().database_url).set(database="postgres")
    return create_engine(url, isolation_level="AUTOCOMMIT")


@pytest.fixture
def scratch(db_available: bool):
    """an empty database of its own, dropped afterwards."""
    if not db_available:
        pytest.skip("postgres not reachable on localhost:5433, run `make up` first")
    name = f"schema_test_{uuid.uuid4().hex[:8]}"
    admin = _admin()
    with admin.connect() as conn:
        conn.execute(text(f'create database "{name}"'))
    url = make_url(get_settings().database_url).set(database=name).render_as_string(False)
    engine = create_engine(url, connect_args=connect_args(url))
    yield engine
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'drop database "{name}" with (force)'))
    admin.dispose()


def differences(engine: Engine) -> list:
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={
                "include_schemas": True,
                "include_name": lambda name, type_, _: (
                    name in (ONBOARDING_SCHEMA, TARGET_SCHEMA) if type_ == "schema" else True
                ),
                "compare_type": True,
                "version_table_schema": ONBOARDING_SCHEMA,
            },
        )
        return compare_metadata(context, Base.metadata)


def revision_of(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return current_revision(conn)


def test_an_empty_database_is_migrated_to_exactly_the_models(scratch) -> None:
    assert upgrade(scratch) == head_revision()
    assert differences(scratch) == []


def test_migrating_twice_changes_nothing_the_second_time(scratch) -> None:
    first = upgrade(scratch)
    assert upgrade(scratch) == first
    assert differences(scratch) == []


def test_a_database_from_before_migrations_is_stamped_and_keeps_its_rows(scratch) -> None:
    """what happened to the live database: tables made by create_all, data in them."""
    with scratch.begin() as conn:
        for schema in (ONBOARDING_SCHEMA, TARGET_SCHEMA):
            conn.execute(text(f'create schema "{schema}"'))
    # the baseline is what create_all built; later revisions are applied on top of it
    Base.metadata.create_all(scratch)
    project_id = uuid.uuid4()
    with scratch.begin() as conn:
        conn.execute(
            text(
                "insert into onboarding.projects (id, customer_name, project_name, source_systems,"
                " target_environment, stage) values (:id, 'Apex', 'kept', '[]', 'staging',"
                " 'reported')"
            ),
            {"id": project_id},
        )

    assert upgrade(scratch) == head_revision()
    with scratch.connect() as conn:
        kept = conn.execute(
            text("select project_name, stage from onboarding.projects where id = :id"),
            {"id": project_id},
        ).one()
    assert tuple(kept) == ("kept", "reported")


def test_the_migrations_go_down_to_nothing_and_back_up(scratch) -> None:
    from alembic import command

    upgrade(scratch)
    with scratch.begin() as conn:
        command.downgrade(alembic_config(conn), "base")
    with scratch.connect() as conn:
        tables = conn.execute(
            text(
                "select count(*) from information_schema.tables where table_schema in "
                "('onboarding', 'target') and table_name <> 'alembic_version'"
            )
        ).scalar_one()
    assert tables == 0
    assert upgrade(scratch) == head_revision()
    assert differences(scratch) == []


def test_the_baseline_is_the_first_revision() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_base() == BASELINE_REVISION
