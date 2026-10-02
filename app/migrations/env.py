"""alembic environment.

The url and the metadata come from the app, so migrations always run against the database the app
is configured for and are compared with the same models. Only the app's two schemas are looked at;
anything else in the database is not ours to migrate.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, create_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  registers every table on Base.metadata
from app.core.config import get_settings
from app.core.db import ONBOARDING_SCHEMA, TARGET_SCHEMA, Base, connect_args

SCHEMAS = {ONBOARDING_SCHEMA, TARGET_SCHEMA}


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    if type_ == "schema":
        return name in SCHEMAS
    return True


def configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=Base.metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema=ONBOARDING_SCHEMA,
        compare_type=True,
        **kwargs,
    )


def run_online(connection: Connection) -> None:
    configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    # `alembic upgrade head --sql`: the script a dba would review, nothing executed
    configure(url=get_settings().database_url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif (shared := context.config.attributes.get("connection")) is not None:
    # the app hands over its own connection, already holding the migration lock
    run_online(shared)
else:
    url = get_settings().database_url
    engine = create_engine(url, poolclass=NullPool, connect_args=connect_args(url))
    with engine.connect() as connection:
        run_online(connection)
        connection.commit()
    engine.dispose()
