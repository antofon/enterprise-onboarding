"""the transformation plan, the validation pass and the dry run, over the committed sample.

These run the real stages against the real sample customer: 9,259 source rows through four
datasets. The numbers asserted here are the ones the sample actually produces, so a change in a
converter or a rule shows up as a changed count rather than as a vague pass.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.http import get_http_client
from app.models.mapping import FieldMapping, MappingStatus
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
    """a profiled, fully reviewed project, and the http client the app should use."""
    client.app.dependency_overrides[get_http_client] = lambda: client
    project = ready_project(session, datasets=SMALL)
    yield project
    client.app.dependency_overrides.clear()


def ids(response) -> list[str]:
    return [row["record_id"] for row in response.json()]


# --- the plan ------------------------------------------------------------------------------------


def test_the_plan_says_what_will_happen_to_every_approved_column(client, ready) -> None:
    plan = client.get(f"/api/v1/projects/{ready.id}/transformation-plan")
    assert plan.status_code == 200
    body = plan.json()
    assert body["customer"] == "Apex Equipment Services"
    assert body["entities"] == ["organization", "contact"]
    assert "dormant_account_inactive" in body["record_rules"]
    assert "primary_from_account_email" in body["cross_dataset_rules"]

    datasets = {d["dataset"]: d for d in body["datasets"]}
    organizations = datasets["organizations.csv"]
    assert organizations["emits"] == ["organization"]
    rules = {r["source_field"]: r for r in organizations["field_rules"]}
    assert rules["acct_num"]["target_path"] == "organization.organization_id"
    assert rules["acct_num"]["converters"][-1] == "identifier"
    assert rules["created_on"]["converters"][-1] == "date"
    assert rules["company_status"]["value_map"] == "organization.lifecycle_status"

    # the account row names a primary contact, which is context for the contacts, not a record
    context = {r["source_field"]: r["target_path"] for r in organizations["context_rules"]}
    assert context == {"primary_contact_email": "contact.email"}

    # one name column fills two target fields
    contacts = {r["source_field"]: r for r in datasets["contacts.csv"]["field_rules"]}
    assert contacts["full_name"]["emits"] == ["contact.first_name", "contact.last_name"]

    # columns with no home in Meridian are listed with the reason they are not travelling
    dropped = {d["source_field"] for d in organizations["dropped"]}
    assert {"account_owner", "customer_tier", "notes"} <= dropped


def test_a_project_with_undecided_mappings_cannot_be_transformed(client, session) -> None:
    client.app.dependency_overrides[get_http_client] = lambda: client
    project = ready_project(session, datasets=SMALL)
    # put one column back to undecided, the way reopening a mapping does
    mapping = session.execute(
        select(FieldMapping).where(FieldMapping.project_id == project.id).limit(1)
    ).scalar_one()
    mapping.status = MappingStatus.suggested
    mapping.decided_by = None
    session.commit()

    refused = client.get(f"/api/v1/projects/{project.id}/transformation-plan")
    assert refused.status_code == 409
    assert refused.json()["error"]["type"] == "invalid_state"
    assert refused.json()["error"]["details"]["undecided_count"] == 1
    assert client.post(f"/api/v1/projects/{project.id}/validate", json={}).status_code == 409
    client.app.dependency_overrides.clear()


def test_a_project_with_no_mappings_at_all_says_so(client, session) -> None:
    project = seed_project(session, datasets=SMALL)
    refused = client.get(f"/api/v1/projects/{project.id}/transformation-plan")
    assert refused.status_code == 409
    assert "mapping step" in refused.json()["error"]["message"]


# --- validation ----------------------------------------------------------------------------------


def test_a_validation_pass_counts_every_record_and_writes_nothing(client, ready) -> None:
    run = client.post(f"/api/v1/projects/{ready.id}/validate", json={})
    assert run.status_code == 200
    body = run.json()
    assert body["kind"] == "validation"
    assert body["status"] == "completed"
    assert body["namespace"] is None
    assert body["config_version"]
    assert body["as_of"] == "2026-10-01"

    organizations = body["stats"]["organization"]
    assert organizations["source_rows"] == 1030
    assert organizations["built"] == 1030
    assert organizations["valid"] + organizations["invalid"] == 1030
    assert organizations["valid"] == 889

    assert body["totals"]["valid"] == 2591
    assert body["issue_counts"]["missing_required"] > 0
    # the customer's rules say how many records they touched
    assert body["applied_rules"]["dormant_account_inactive (customer rule 2)"] == 469
    # and normalization says what it changed, aggregated
    assert any("assumed +1 country code" in note for note in body["normalizations"])

    # nothing reached the target platform
    auth = {"Authorization": f"Bearer {get_settings().target_api_token}"}
    assert client.get("/target/v1/counts", headers=auth).json()["counts"]["organization"] == 0
    assert client.get(f"/api/v1/projects/{ready.id}").json()["stage"] == "validated"


def test_the_issues_can_be_read_back_and_filtered(client, ready) -> None:
    run = client.post(f"/api/v1/projects/{ready.id}/validate", json={}).json()
    issues = client.get(f"/api/v1/projects/{ready.id}/migrations/{run['id']}/issues?limit=5")
    assert issues.status_code == 200
    assert len(issues.json()) == 5
    assert all(i["severity"] == "error" for i in issues.json())

    warnings = client.get(
        f"/api/v1/projects/{ready.id}/migrations/{run['id']}/issues?severity=warning&limit=5"
    ).json()
    assert warnings and all(w["severity"] == "warning" for w in warnings)

    one_type = client.get(
        f"/api/v1/projects/{ready.id}/migrations/{run['id']}/issues?error_type=malformed_email"
    ).json()
    assert one_type and all(i["entity"] == "contact" for i in one_type)
    assert all(i["field"] == "email" for i in one_type)
    # an issue names the row it came from so somebody can go and look
    assert one_type[0]["dataset"] == "contacts.csv"
    assert one_type[0]["source_row"] > 0
    assert one_type[0]["value"]

    breakdown = client.get(
        f"/api/v1/projects/{ready.id}/migrations/{run['id']}/issue-breakdown"
    ).json()
    assert breakdown[0]["count"] >= breakdown[-1]["count"]
    assert {"entity", "severity", "error_type", "count", "fields"} == set(breakdown[0])


def test_validation_can_be_narrowed_to_one_entity(client, ready) -> None:
    run = client.post(
        f"/api/v1/projects/{ready.id}/validate", json={"entities": ["organization"]}
    ).json()
    assert run["options"]["entities"] == ["organization"]
    assert set(run["stats"]) == {"organization", "contact"}


# --- the dry run ---------------------------------------------------------------------------------


def test_a_dry_run_writes_into_its_own_namespace_and_reconciles(client, ready) -> None:
    run = client.post(
        f"/api/v1/projects/{ready.id}/migrations/dry-run", json={"limit_per_entity": 40}
    )
    assert run.status_code == 200
    body = run.json()
    assert body["kind"] == "dry_run"
    assert body["status"] == "completed"
    namespace = body["namespace"]
    assert namespace and namespace != str(uuid.UUID(int=0))

    organizations = body["stats"]["organization"]
    assert organizations["attempted"] == 40
    assert organizations["accepted"] == 40
    assert organizations["created"] == 40
    assert organizations["rejected"] == 0
    assert organizations["failed"] == 0

    # what the target holds is what the run says it accepted
    assert body["target_counts"]["organization"] == organizations["accepted"]
    assert body["target_counts"]["contact"] == body["stats"]["contact"]["accepted"]

    # and the live data is still empty: a rehearsal cannot touch production
    auth = {"Authorization": f"Bearer {get_settings().target_api_token}"}
    assert client.get("/target/v1/counts", headers=auth).json()["counts"]["organization"] == 0

    stored = client.get(
        "/target/v1/organizations", headers={**auth, "X-Meridian-Namespace": namespace}
    )
    assert len(stored.json()) == 40
    assert client.get(f"/api/v1/projects/{ready.id}").json()["stage"] == "dry_run_complete"


def test_a_child_whose_organization_is_not_in_the_namespace_is_never_attempted(
    client, ready
) -> None:
    body = client.post(
        f"/api/v1/projects/{ready.id}/migrations/dry-run", json={"limit_per_entity": 40}
    ).json()
    contacts = body["stats"]["contact"]
    assert contacts["blocked"] > 0
    assert contacts["attempted"] == contacts["accepted"]

    blocked = client.get(
        f"/api/v1/projects/{ready.id}/migrations/{body['id']}/failures?error_type=parent_not_in_target"
    ).json()
    assert blocked
    first = blocked[0]
    assert first["stage"] == "blocked"
    assert first["attempts"] == 0
    assert first["http_status"] is None
    assert "did not write it" in first["message"]


def test_the_staging_namespace_can_be_thrown_away_with_the_run(client, ready) -> None:
    body = client.post(
        f"/api/v1/projects/{ready.id}/migrations/dry-run",
        json={"limit_per_entity": 10, "purge_namespace_after": True},
    ).json()
    assert body["stats"]["organization"]["accepted"] == 10
    assert body["target_counts"] == {}
    auth = {
        "Authorization": f"Bearer {get_settings().target_api_token}",
        "X-Meridian-Namespace": body["namespace"],
    }
    assert client.get("/target/v1/counts", headers=auth).json()["counts"]["organization"] == 0


def test_two_dry_runs_do_not_see_each_other(client, ready) -> None:
    first = client.post(
        f"/api/v1/projects/{ready.id}/migrations/dry-run", json={"limit_per_entity": 5}
    ).json()
    second = client.post(
        f"/api/v1/projects/{ready.id}/migrations/dry-run", json={"limit_per_entity": 5}
    ).json()
    assert first["namespace"] != second["namespace"]
    assert first["stats"]["organization"]["created"] == 5
    # the same five records again, in a fresh namespace, are creates rather than conflicts
    assert second["stats"]["organization"]["created"] == 5
    assert second["stats"]["organization"]["rejected"] == 0

    runs = client.get(f"/api/v1/projects/{ready.id}/migrations").json()
    assert [r["kind"] for r in runs] == ["dry_run", "dry_run"]
    assert runs[0]["started_at"] >= runs[1]["started_at"]


def test_a_failing_target_is_retried_and_then_reported(client, ready, monkeypatch) -> None:
    """every write to this run fails with a 500. the records are retried, the run still completes,
    and each failure is stored with the status and the target's own message."""
    settings = get_settings()
    monkeypatch.setattr(settings, "target_fault_rate", 1.0)
    monkeypatch.setattr(settings, "target_fault_modes", "server_error")
    monkeypatch.setattr(settings, "target_max_attempts", 2)
    monkeypatch.setattr(settings, "target_retry_backoff_seconds", 0.0)

    body = client.post(
        f"/api/v1/projects/{ready.id}/migrations/dry-run", json={"limit_per_entity": 3}
    ).json()
    assert body["status"] == "completed"
    organizations = body["stats"]["organization"]
    assert organizations["attempted"] == 3
    assert organizations["accepted"] == 0
    assert organizations["failed"] == 3
    assert organizations["retries"] == 3  # one retry each
    assert body["target_counts"]["organization"] == 0

    failures = client.get(
        f"/api/v1/projects/{ready.id}/migrations/{body['id']}/failures?entity=organization"
    ).json()
    assert len(failures) == 3
    assert {f["error_type"] for f in failures} == {"target_unavailable"}
    assert {f["http_status"] for f in failures} == {500}
    assert all(f["attempts"] == 2 for f in failures)
    assert all(f["request_id"] for f in failures)
    # with no organizations in the target, their contacts were not attempted
    assert body["stats"]["contact"]["blocked"] > 0


def test_a_malformed_answer_from_the_target_is_a_failure_not_a_success(
    client, ready, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "target_fault_rate", 1.0)
    monkeypatch.setattr(settings, "target_fault_modes", "malformed")
    monkeypatch.setattr(settings, "target_max_attempts", 1)

    body = client.post(
        f"/api/v1/projects/{ready.id}/migrations/dry-run", json={"limit_per_entity": 2}
    ).json()
    failures = client.get(
        f"/api/v1/projects/{ready.id}/migrations/{body['id']}/failures?entity=organization"
    ).json()
    assert {f["error_type"] for f in failures} == {"malformed_response"}
    # the write actually landed, which is why the id contract matters: a retry would be a no-op
    assert body["target_counts"]["organization"] == 2
    assert body["stats"]["organization"]["accepted"] == 0


def test_a_run_can_give_up_after_a_number_of_failures(client, ready, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "target_fault_rate", 1.0)
    monkeypatch.setattr(settings, "target_fault_modes", "server_error")
    monkeypatch.setattr(settings, "target_max_attempts", 1)

    body = client.post(
        f"/api/v1/projects/{ready.id}/migrations/dry-run",
        json={"limit_per_entity": 50, "stop_after_failures": 4},
    ).json()
    assert body["stats"]["organization"]["attempted"] == 4
    assert body["stats"]["organization"]["failed"] == 4


def test_an_unknown_run_is_a_404(client, ready) -> None:
    missing = client.get(f"/api/v1/projects/{ready.id}/migrations/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["type"] == "not_found"
