"""test wiring. unit tests need nothing. integration tests need the compose postgres on
localhost:5433 and use their own database (onboarding_test, created by deploy/postgres-init)."""

from __future__ import annotations

import os

# set before anything imports app.core.config, which caches settings on first use
os.environ["APP_ENV"] = "test"
os.environ["LOG_FORMAT"] = "console"
os.environ["LLM_PROVIDER"] = os.environ.get("TEST_LLM_PROVIDER", "none")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://onboarding:onboarding@localhost:5433/onboarding_test"
)
# the migration layer reaches the target platform over http. under TestClient that is the app
# itself, so tests point it at testserver and hand the loader the test client.
os.environ["TARGET_API_BASE_URL"] = "http://testserver/target/v1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def db_available() -> bool:
    from app.core.db import check_db

    return check_db()


@pytest.fixture
def client(db_available: bool):
    """a fresh app + clean tables per test. skips when postgres is not up."""
    if not db_available:
        pytest.skip("postgres not reachable on localhost:5433, run `make up` first")

    from app.core.db import Base, get_engine, init_db
    from app.main import create_app

    init_db()
    with TestClient(create_app()) as test_client:
        yield test_client
    Base.metadata.drop_all(get_engine())
