"""the logs, read the way somebody diagnosing a failed onboarding would read them.

The api runs with LOG_FORMAT=json in compose. These tests switch the same configuration on, drive
real requests, and parse every line: each must be json, each pipeline step must say how it ended
with the context the spec asks for (project_id, migration_run_id, stage, dataset or entity,
record_count, duration_ms, error_type), and every line of a request must carry its request id.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.db import get_db, get_session_factory
from app.core.http import get_http_client
from app.core.logging import configure_logging
from tests.golden import ready_project, seed_project

pytestmark = pytest.mark.integration

SMALL = ("organizations.csv", "contacts.csv")


class Logs:
    def __init__(self, buffer: io.StringIO) -> None:
        self.buffer = buffer

    def lines(self) -> list[dict[str, Any]]:
        raw = [line for line in self.buffer.getvalue().splitlines() if line.strip()]
        # every line is json, or the test fails on the one that is not
        return [json.loads(line) for line in raw]

    def events(self, event: str, **match: Any) -> list[dict[str, Any]]:
        return [
            line
            for line in self.lines()
            if line.get("event") == event and all(line.get(k) == v for k, v in match.items())
        ]

    def clear(self) -> None:
        self.buffer.seek(0)
        self.buffer.truncate()


@pytest.fixture
def logs(client):
    """json logging into a buffer for the length of one test. depends on `client` so the app's
    own startup has configured logging first and this replaces it."""
    settings = get_settings()
    previous = settings.log_format
    settings.log_format = "json"
    buffer = io.StringIO()
    configure_logging(force=True, stream=buffer)
    yield Logs(buffer)
    settings.log_format = previous
    configure_logging(force=True)


@pytest.fixture
def session():
    db = get_session_factory()()
    yield db
    db.close()


def test_a_dry_run_logs_every_step_with_the_context_needed_to_diagnose_it(
    client, session, logs
) -> None:
    client.app.dependency_overrides[get_http_client] = lambda: client
    project = ready_project(session, datasets=SMALL)
    logs.clear()

    response = client.post(
        f"/api/v1/projects/{project.id}/migrations/dry-run",
        json={"limit_per_entity": 25},
        headers={"x-request-id": "diag-0001"},
    )
    assert response.status_code == 200
    run_id = response.json()["id"]
    assert response.headers["x-request-id"] == "diag-0001"

    # the runner's own lines (not the target api's, which answer requests of their own)
    ours = [line for line in logs.lines() if line.get("request_id") == "diag-0001"]
    assert ours, "nothing was logged under the caller's request id"
    finished = {
        (line["stage"], line.get("entity")): line
        for line in ours
        if line["event"] == "stage_finished"
    }
    expected = {
        ("transform", None),
        ("validate", None),
        ("write", "organization"),
        ("write", "contact"),
        ("reconcile", None),
        ("dry_run", None),
    }
    assert expected <= set(finished), sorted(finished)
    for line in finished.values():
        assert line["project_id"] == str(project.id)
        assert isinstance(line["duration_ms"], float)
        assert isinstance(line["record_count"], int)
        assert line["level"] == "info"
        assert line["timestamp"].endswith("Z")
    # once the run exists, every step inside it carries its id
    for key in expected - {("transform", None), ("validate", None)}:
        assert finished[key]["migration_run_id"] == run_id
    assert finished[("transform", None)]["migration_run_id"] == run_id

    # the write steps say what happened to the records, by entity
    organizations = finished[("write", "organization")]
    assert organizations["record_count"] == 25
    assert organizations["accepted"] == 25
    assert organizations["not_attempted"] > 0
    assert finished[("dry_run", None)]["accepted"] >= 25
    assert finished[("reconcile", None)]["status"] == "balanced"

    # one line per request, with the status and how long it took
    request = logs.events("request", request_id="diag-0001")
    assert len(request) == 1
    assert request[0]["status_code"] == 200
    assert request[0]["duration_ms"] > 0
    client.app.dependency_overrides.clear()


def test_a_failing_step_says_which_step_and_what_kind_of_failure(client, session, logs) -> None:
    """the billing feed loses its export and answers 502: the profile step fails with the api's
    own error type, the request answers 502, and both lines carry the same request id."""
    client.app.dependency_overrides[get_http_client] = lambda: client
    project = seed_project(session, datasets=("subscriptions (billing api)",))
    settings = get_settings()
    export = settings.billing_source_file
    settings.billing_source_file = "sample_customer/data/gone.json"
    logs.clear()
    try:
        response = client.post(f"/api/v1/projects/{project.id}/sources/profile")
    finally:
        settings.billing_source_file = export
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["type"] == "source_unavailable"
    assert error["request_id"] == response.headers["x-request-id"]

    failed = logs.events("stage_failed", stage="profile")
    assert len(failed) == 1
    assert failed[0]["error_type"] == "source_unavailable"
    assert failed[0]["project_id"] == str(project.id)
    assert failed[0]["request_id"] == error["request_id"]
    # an answer the api gives on purpose is a warning without a traceback
    assert failed[0]["level"] == "warning"
    assert "exception" not in failed[0]
    client.app.dependency_overrides.clear()


def test_an_unhandled_error_keeps_the_envelope_and_leaves_the_traceback_in_the_log(
    client, logs
) -> None:
    def boom() -> None:
        raise RuntimeError("a bug nobody planned for")

    client.app.add_api_route("/boom", boom, methods=["GET"])
    response = client.get("/boom", headers={"x-request-id": "boom-1"})
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["type"] == "internal_error"
    assert error["request_id"] == "boom-1"
    # the caller gets the id, not the internals
    assert "nobody planned" not in response.text
    assert response.headers["x-request-id"] == "boom-1"

    logged = logs.events("unhandled_error", request_id="boom-1")
    assert len(logged) == 1
    assert logged[0]["level"] == "error"
    assert logged[0]["error_type"] == "RuntimeError"
    assert "a bug nobody planned for" in logged[0]["exception"]
    assert logs.events("request", request_id="boom-1")[0]["status_code"] == 500


def test_the_database_being_down_is_a_503_the_caller_can_wait_out(client, logs) -> None:
    # a session bound to a port nothing listens on: every query fails to connect
    dead = create_engine("postgresql+psycopg://onboarding:x@127.0.0.1:1/none", pool_pre_ping=False)
    factory = sessionmaker(bind=dead)

    def unreachable():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    client.app.dependency_overrides[get_db] = unreachable
    try:
        response = client.get("/api/v1/projects")
    finally:
        client.app.dependency_overrides.clear()
        dead.dispose()
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "database_unavailable"
    assert logs.events("database_unavailable")


def test_a_request_id_that_does_not_look_like_one_is_replaced(client) -> None:
    hostile = 'x" event=forged level=info'
    response = client.get("/api/v1/projects", headers={"x-request-id": hostile})
    assert response.status_code == 200
    assert response.headers["x-request-id"] != hostile
    assert len(response.headers["x-request-id"]) == 12
    kept = client.get("/api/v1/projects", headers={"x-request-id": "dryrun-1a2b3c4d-AX-000123"})
    assert kept.headers["x-request-id"] == "dryrun-1a2b3c4d-AX-000123"


def test_errors_from_the_framework_carry_the_request_id_too(client) -> None:
    missing = client.get("/api/v1/nothing-here", headers={"x-request-id": "nf-1"})
    assert missing.status_code == 404
    assert missing.json()["error"]["request_id"] == "nf-1"
    shape = client.post("/api/v1/projects", json={}, headers={"x-request-id": "shape-1"})
    assert shape.status_code == 422
    assert shape.json()["error"]["request_id"] == "shape-1"
