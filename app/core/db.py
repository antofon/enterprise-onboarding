"""sqlalchemy engine + session plumbing.

one postgres, two schemas:
  onboarding  - this app's own tables (projects, datasets, mappings, runs, reports)
  target      - the fictional saas platform the migration writes into, behind its own api
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

ONBOARDING_SCHEMA = "onboarding"
TARGET_SCHEMA = "target"


class Base(DeclarativeBase):
    """base for the onboarding-side tables."""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def connect_args(url: str) -> dict[str, str]:
    """the database user and the app's schema are both called `onboarding`, so postgres' default
    search_path (`"$user", public`) would quietly make the app's schema the default one. Every
    table is schema-qualified, so nothing needs the search path; pinning it to `public` keeps
    reflection (alembic's autogenerate) from mistaking `onboarding` for the default schema."""
    return {"options": "-c search_path=public"} if url.startswith("postgresql") else {}


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        url = get_settings().database_url
        _engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args(url))
        _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def reset_engine() -> None:
    """tests swap database urls; this forgets the cached engine so the next call rebuilds it."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_db() -> Iterator[Session]:
    """fastapi dependency: one session per request."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


# the type to use on route signatures: `session: DbSession`
DbSession = Annotated[Session, Depends(get_db)]


def check_db() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("select 1"))
        return True
    except Exception:  # noqa: BLE001 - health check, any failure means "not ok"
        return False


def init_db() -> str | None:
    """bring the database to the newest schema revision (alembic, see app/core/migrations.py)
    and return the revision it is at."""
    from app.core.migrations import upgrade

    return upgrade(get_engine())
