"""what happens when something goes wrong, driven for real.

One test per row of the failure table in docs/ARCHITECTURE.md that is not already covered where
its stage is tested. Each one breaks something the way it breaks in a real onboarding (a feed
that rate-limits, a target that is down or busy, a run that crashes after its writes, a database
that drops mid-run, a process that dies and leaves its run behind, a model that does not answer)
and checks what is left behind: the http answer, the run row, the stage, the reconciliation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.ai.provider import LlmError, get_llm_provider
from app.api import billing as billing_api
from app.api import target as target_api
from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.http import get_http_client
from app.models.migration import MigrationRun, RunKind, RunStatus
from app.models.project import SourceDataset
from app.services import migration as migration_service
from tests.golden import ready_project, seed_project

pytestmark = pytest.mark.integration

SMALL = ("organizations.csv", "contacts.csv")


@pytest.fixture
def session():
    db = get_session_factory()()
    yield db
    db.close()


@pytest.fixture
def ready(client, session):
    client.app.dependency_overrides[get_http_client] = lambda: client
    project = ready_project(session, datasets=SMALL)
    target_api.attempts_seen.reset()
    yield project
    client.app.dependency_overrides.clear()


@pytest.fixture
def fast(monkeypatch):
    """retries without the waiting, so the suite stays quick. the waits themselves are unit
    tested against a recorded sleep."""
    settings = get_settings()
    monkeypatch.setattr(settings, "target_retry_backoff_seconds", 0.0)
    monkeypatch.setattr(settings, "billing_retry_backoff_seconds", 0.0)
    return settings


def stage_of(client, project) -> str:
    return client.get(f"/api/v1/projects/{project.id}").json()["stage"]


def latest_dry_run(client, project) -> dict:
    runs = client.get(f"/api/v1/projects/{project.id}/migrations?kind=dry_run").json()
    return client.get(f"/api/v1/projects/{project.id}/migrations/{runs[0]['id']}").json()


# --- the customer's billing feed ----------------------------------------------------------------


def test_a_feed_that_rate_limits_is_waited_out_and_every_record_arrives(
    client, session, monkeypatch
) -> None:
    """LegacyBill allows two requests a second here; the profile pages through six pages, gets
    turned away, waits what Retry-After says, and ends with all 1,003 subscriptions."""
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_rate_limit", 2)
    monkeypatch.setattr(settings, "billing_rate_window_seconds", 1.0)
    billing_api.rate_window.reset()
    client.app.dependency_overrides[get_http_client] = lambda: client
    project = seed_project(session, datasets=("subscriptions (billing api)",))

    response = client.post(f"/api/v1/projects/{project.id}/sources/profile")
    client.app.dependency_overrides.clear()
    billing_api.rate_window.reset()

    assert response.status_code == 200, response.text
    assert response.json()["datasets"][0]["row_count"] == 1003
    assert stage_of(client, project) == "profiled"


def test_the_mock_feed_says_how_long_to_wait(client, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_rate_limit", 1)
    monkeypatch.setattr(settings, "billing_rate_window_seconds", 30.0)
    billing_api.rate_window.reset()
    token = {"Authorization": f"Bearer {settings.billing_api_token}"}
    assert client.get("/mock/billing/v1/subscriptions", headers=token).status_code == 200
    turned_away = client.get("/mock/billing/v1/subscriptions", headers=token)
    billing_api.rate_window.reset()
    assert turned_away.status_code == 429
    assert 1 <= int(turned_away.headers["retry-after"]) <= 30
    assert turned_away.json()["error"]["type"] == "rate_limited"


def test_a_feed_that_stays_down_fails_profiling_after_its_retries_and_moves_nothing(
    client, session, fast, monkeypatch
) -> None:
    monkeypatch.setattr(fast, "billing_source_file", "sample_customer/data/gone.json")
    client.app.dependency_overrides[get_http_client] = lambda: client
    project = seed_project(session, datasets=("subscriptions (billing api)",))
    response = client.post(f"/api/v1/projects/{project.id}/sources/profile")
    client.app.dependency_overrides.clear()

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["type"] == "source_unavailable"
    assert error["details"]["attempts"] == fast.billing_max_attempts
    assert error["details"]["page"] == 1
    assert stage_of(client, project) == "created"


def test_a_source_that_disappears_before_the_dry_run_fails_the_run_not_the_api(
    client, session, ready
) -> None:
    dataset = session.execute(
        select(SourceDataset).where(
            SourceDataset.project_id == ready.id, SourceDataset.name == "contacts.csv"
        )
    ).scalar_one()
    dataset.location = "sample_customer/data/contacts_moved.csv"
    session.commit()
    before = stage_of(client, ready)

    response = client.post(f"/api/v1/projects/{ready.id}/migrations/dry-run", json={})
    assert response.status_code == 404
    assert response.json()["error"]["request_id"]

    run = latest_dry_run(client, ready)
    assert run["status"] == "failed"
    assert "contacts_moved.csv" in run["error"]
    assert run["finished_at"] is not None
    assert stage_of(client, ready) == before


# --- the target platform ------------------------------------------------------------------------


def test_a_target_that_is_down_opens_the_breaker_instead_of_retrying_every_record(
    client, ready, fast, monkeypatch
) -> None:
    """the target's address refuses connections. without the breaker this run would retry all
    889 organizations three times each before blocking every child."""
    monkeypatch.setattr(fast, "target_api_base_url", "http://127.0.0.1:9/target/v1")
    real = httpx.Client(timeout=2.0)
    client.app.dependency_overrides[get_http_client] = lambda: real
    try:
        response = client.post(f"/api/v1/projects/{ready.id}/migrations/dry-run", json={})
    finally:
        client.app.dependency_overrides[get_http_client] = lambda: client
        real.close()

    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "failed"
    assert "10 records in a row" in run["error"]
    assert "transport_error" in run["error"]

    organizations = run["stats"]["organization"]
    assert organizations["attempted"] == fast.target_breaker_threshold
    assert organizations["failed"] == fast.target_breaker_threshold
    assert organizations["retries"] == 2 * fast.target_breaker_threshold
    assert organizations["not_attempted"] == organizations["valid"] - organizations["attempted"]
    contacts = run["stats"]["contact"]
    assert contacts["attempted"] == 0 and contacts["not_attempted"] == contacts["valid"]

    # the run still accounted for every record, and says it could not read the target back
    reconciliation = client.get(
        f"/api/v1/projects/{ready.id}/migrations/{run['id']}/reconciliation"
    ).json()
    assert reconciliation["status"] == "discrepancies"
    failed = {c["check"] for c in reconciliation["checks"] if not c["ok"]}
    assert failed == {"target_readable"}

    failures = client.get(f"/api/v1/projects/{ready.id}/migrations/{run['id']}/failures").json()
    assert len(failures) == fast.target_breaker_threshold
    assert {f["error_type"] for f in failures} == {"transport_error"}
    # a rehearsal that never happened does not move the project on
    assert stage_of(client, ready) == "ready_to_transform"


@pytest.mark.parametrize("mode", ["rate_limited", "server_error"])
def test_a_target_that_fails_each_record_once_is_retried_through_and_balances(
    client, ready, fast, monkeypatch, mode
) -> None:
    """every record's first attempt is turned away (a 429 with Retry-After, or a 500) and the
    retry gets through: everything lands, each record cost one retry, nothing is doubled."""
    monkeypatch.setattr(fast, "target_fault_rate", 1.0)
    monkeypatch.setattr(fast, "target_fault_modes", mode)
    monkeypatch.setattr(fast, "target_fault_attempts", 1)
    monkeypatch.setattr(fast, "target_fault_retry_after_seconds", 0)

    response = client.post(
        f"/api/v1/projects/{ready.id}/migrations/dry-run", json={"limit_per_entity": 20}
    )
    run = response.json()
    assert run["status"] == "completed", run.get("error")
    for entity in ("organization", "contact"):
        stats = run["stats"][entity]
        assert stats["failed"] == 0 and stats["rejected"] == 0
        assert stats["retries"] == stats["attempted"]
        assert stats["accepted"] == stats["attempted"]
    reconciliation = client.get(
        f"/api/v1/projects/{ready.id}/migrations/{run['id']}/reconciliation"
    ).json()
    assert reconciliation["status"] == "balanced"


def test_a_purge_that_fails_is_reported_on_the_run_and_does_not_fail_it(
    client, ready, monkeypatch
) -> None:
    def refuse(self) -> None:
        raise httpx.ConnectError("target went away")

    monkeypatch.setattr(migration_service.TargetClient, "purge", refuse)
    run = client.post(
        f"/api/v1/projects/{ready.id}/migrations/dry-run",
        json={"limit_per_entity": 5, "purge_namespace_after": True},
    ).json()
    assert run["status"] == "completed"
    assert "could not be purged" in run["error"]
    assert run["namespace"] in run["error"]


# --- the run itself -----------------------------------------------------------------------------


def test_a_crash_after_the_writes_marks_the_run_failed_and_keeps_what_landed(
    client, ready, monkeypatch
) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("reconciliation bug")

    monkeypatch.setattr(migration_service, "reconcile_run", broken)
    response = client.post(
        f"/api/v1/projects/{ready.id}/migrations/dry-run",
        json={"limit_per_entity": 5},
        headers={"x-request-id": "crash-1"},
    )
    assert response.status_code == 500
    assert response.json()["error"] == {
        "type": "internal_error",
        "message": "the server hit an unexpected error; quote the request id when reporting it",
        "details": {},
        "request_id": "crash-1",
    }
    run = latest_dry_run(client, ready)
    assert run["status"] == "failed"
    assert run["error"] == "RuntimeError: reconciliation bug"
    # the writes happened before the crash and stay in the namespace for inspection
    auth = {
        "Authorization": f"Bearer {get_settings().target_api_token}",
        "X-Meridian-Namespace": run["namespace"],
    }
    assert client.get("/target/v1/counts", headers=auth).json()["counts"]["organization"] == 5
    assert stage_of(client, ready) == "ready_to_transform"


def test_a_database_error_mid_run_is_rolled_back_and_the_run_still_recorded(
    client, ready, monkeypatch
) -> None:
    def drop(*args, **kwargs):
        raise OperationalError("insert into validation_issues", {}, Exception("server closed"))

    monkeypatch.setattr(migration_service, "_store_issues", drop)
    response = client.post(f"/api/v1/projects/{ready.id}/validate", json={})
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "database_unavailable"

    runs = client.get(f"/api/v1/projects/{ready.id}/migrations?kind=validation").json()
    assert runs[0]["status"] == "failed"
    assert stage_of(client, ready) == "ready_to_transform"


def test_a_run_left_behind_by_a_dead_process_is_marked_failed_when_the_api_starts(
    client, session, ready
) -> None:
    now = datetime.now(UTC)

    def left_behind(minutes_ago: float) -> uuid.UUID:
        run = MigrationRun(
            id=uuid.uuid4(),
            project_id=ready.id,
            kind=RunKind.dry_run,
            status=RunStatus.running,
            namespace=uuid.uuid4(),
            started_at=now - timedelta(minutes=minutes_ago + 1),
            heartbeat_at=now - timedelta(minutes=minutes_ago),
        )
        session.add(run)
        session.commit()
        return run.id

    dead = left_behind(minutes_ago=45)
    alive = left_behind(minutes_ago=0.5)

    # a restart: the new process sweeps on startup
    with TestClient(client.app):
        pass
    session.expire_all()
    swept = session.get(MigrationRun, dead)
    assert swept is not None and swept.status is RunStatus.failed
    assert swept.error is not None and swept.error.startswith("interrupted")
    assert swept.finished_at is not None
    live = session.get(MigrationRun, alive)
    assert live is not None and live.status is RunStatus.running


def test_the_projects_next_run_sweeps_its_abandoned_ones_first(client, session, ready) -> None:
    abandoned = MigrationRun(
        id=uuid.uuid4(),
        project_id=ready.id,
        kind=RunKind.validation,
        status=RunStatus.running,
        started_at=datetime.now(UTC) - timedelta(hours=2),
    )
    session.add(abandoned)
    session.commit()
    assert client.post(f"/api/v1/projects/{ready.id}/validate", json={}).status_code == 200
    session.expire_all()
    found = session.get(MigrationRun, abandoned.id)
    assert found is not None and found.status is RunStatus.failed


# --- the model ----------------------------------------------------------------------------------


class Unreachable:
    name = "anthropic"
    model = "claude-opus-5"

    def complete(self, **_: object):
        raise LlmError("anthropic unreachable: APIConnectionError", details={"provider": "x"})


def test_a_model_that_does_not_answer_is_a_502_and_manual_mode_still_works(client, session) -> None:
    client.app.dependency_overrides[get_http_client] = lambda: client
    project = seed_project(session, datasets=("organizations.csv",))
    assert client.post(f"/api/v1/projects/{project.id}/sources/profile").status_code == 200

    client.app.dependency_overrides[get_llm_provider] = lambda: Unreachable()
    refused = client.post(f"/api/v1/projects/{project.id}/mappings/suggest", json={})
    assert refused.status_code == 502
    assert refused.json()["error"]["type"] == "llm_error"
    calls = client.get(f"/api/v1/projects/{project.id}/llm-calls").json()
    assert calls[0]["status"] == "error" and "unreachable" in calls[0]["error"]
    assert stage_of(client, project) == "profiled"

    # the same project, no model: proposals come from the comparison and the review goes on
    del client.app.dependency_overrides[get_llm_provider]
    manual = client.post(f"/api/v1/projects/{project.id}/mappings/suggest", json={})
    assert manual.status_code == 200
    assert manual.json()["provider"] == "none"
    assert stage_of(client, project) == "mapped"
    client.app.dependency_overrides.clear()


def test_a_model_that_does_not_answer_never_holds_up_the_readiness_report(client, ready) -> None:
    client.post(f"/api/v1/projects/{ready.id}/migrations/dry-run", json={"limit_per_entity": 5})
    client.app.dependency_overrides[get_llm_provider] = lambda: Unreachable()
    response = client.post(f"/api/v1/projects/{ready.id}/reports/readiness", json={})
    del client.app.dependency_overrides[get_llm_provider]
    assert response.status_code == 201, response.text
    report = response.json()
    assert report["summary_origin"] == "template"
    assert "unreachable" in report["content"]["summary"]["note"]


# --- the customer's configuration ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("breakage", "named"),
    [
        (('ontario: "ON"', "ontario: ON"), "boolean"),
        (("converters: [phone_e164]", "converters: [phone_e165]"), "phone_e165"),
    ],
)
def test_a_broken_configuration_fails_before_any_row_is_read(
    client, ready, monkeypatch, tmp_path, breakage, named
) -> None:
    """a yaml boolean where a province code was meant, a converter that does not exist: a 500
    that names the problem, before a run is even recorded."""
    from pathlib import Path

    from app.services.transform.config import get_config

    original = Path("sample_customer/transformation_config.yaml").read_text()
    old, new = breakage
    assert old in original
    broken = tmp_path / "transformation_config.yaml"
    broken.write_text(original.replace(old, new))
    monkeypatch.setattr(get_settings(), "transformation_config", str(broken))
    get_config.cache_clear()
    try:
        response = client.post(f"/api/v1/projects/{ready.id}/validate", json={})
    finally:
        get_config.cache_clear()
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["type"] == "transformation_config_invalid"
    assert named in error["message"] + str(error["details"])
    assert client.get(f"/api/v1/projects/{ready.id}/migrations").json() == []
    assert stage_of(client, ready) == "ready_to_transform"
