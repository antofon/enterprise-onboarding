"""sqlalchemy engine + session plumbing.

one postgres, two schemas:
  onboarding  - this app's own tables (projects, datasets, mappings, runs, reports)
  target      - the fictional saas platform the migration writes into, behind its own api
"""

from __future__ import annotations

from collections.abc import Iterator

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


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
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


def check_db() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("select 1"))
        return True
    except Exception:  # noqa: BLE001 - health check, any failure means "not ok"
        return False


def init_db() -> None:
    """create the two schemas and every registered table. alembic can take over once the
    schema stops moving daily (see docs/BUILD_LOG.md)."""
    engine = get_engine()
    with engine.begin() as conn:
        for schema in (ONBOARDING_SCHEMA, TARGET_SCHEMA):
            conn.execute(text(f'create schema if not exists "{schema}"'))
    import app.models  # noqa: F401  registers the tables on Base.metadata

    Base.metadata.create_all(engine)
