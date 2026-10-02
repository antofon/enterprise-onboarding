"""reconciliation and the readiness report over http, on the committed sample.

The dry runs here are limited slices so the suite stays quick; reconciliation has to balance on
a slice exactly as on a full run, because the records a slice never reaches are counted too.
"""

from __future__ import annotations

import json
import re

import pytest

from app.ai.provider import StructuredResult, get_llm_provider
from app.ai.schemas import ReadinessSummary
from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.http import get_http_client
from tests.golden import ready_project

pytestmark = pytest.mark.integration

SMALL = ("organizations.csv", "contacts.csv")
_FENCE = re.compile(r"```json\n(.*?)\n```", re.S)


@pytest.fixture
def session():
    db = get_session_factory()()
    yield db
    db.close()


@pytest.fixture
def ready(client, session):
    client.app.dependency_overrides[get_http_client] = lambda: client
    project = ready_project(session, datasets=SMALL)
    yield project
    client.app.dependency_overrides.clear()


def auth(namespace: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {get_settings().target_api_token}"}
    if namespace:
        headers["X-Meridian-Namespace"] = namespace
    return headers


def dry_run(client, project, **body) -> dict:
    response = client.post(f"/api/v1/projects/{project.id}/migrations/dry-run", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --- reconciliation ------------------------------------------------------------------------------


def test_a_dry_run_reconciles_itself_and_balances(client, ready) -> None:
    run = dry_run(client, ready, limit_per_entity=40)
    rec = client.get(f"/api/v1/projects/{ready.id}/migrations/{run['id']}/reconciliation")
    assert rec.status_code == 200
    body = rec.json()
    assert body["trigger"] == "dry_run"
    assert body["status"] == "balanced", body["discrepancies"]
    assert body["discrepancies"] == []
    assert all(c["ok"] for c in body["checks"])

    organizations = body["entities"]["organization"]
    assert organizations["source_rows"] == 1030
    assert organizations["built"] == 1030
    assert organizations["valid"] == 889
    # the slice: 40 sent, the rest of the valid records counted as not reached
    assert organizations["attempted"] == 40
    assert organizations["not_attempted"] == 889 - 40
    assert organizations["accepted"] == organizations["in_target"] == 40
    # every excluded record has its reason, and the reasons add up
    assert sum(organizations["excluded_by_reason"].values()) == organizations["excluded"] == 141
    assert organizations["excluded_by_reason"]["missing_required"] > 0
    # 1,030 rows, fewer distinct account numbers: the duplicates and the blanks
    assert organizations["distinct_ids"] < 1030

    contacts = body["entities"]["contact"]
    assert (
        contacts["valid"] == contacts["attempted"] + contacts["blocked"] + contacts["not_attempted"]
    )
    assert body["target_counts"]["organization"] == 40
    assert body["totals"]["accepted"] == body["totals"]["in_target"]


def test_a_write_that_landed_but_was_reported_failed_is_a_discrepancy(
    client, ready, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "target_fault_rate", 1.0)
    monkeypatch.setattr(settings, "target_fault_modes", "malformed")
    monkeypatch.setattr(settings, "target_max_attempts", 1)

    run = dry_run(client, ready, limit_per_entity=2, entities=["organization"])
    body = client.get(f"/api/v1/projects/{ready.id}/migrations/{run['id']}/reconciliation").json()
    assert body["status"] == "discrepancies"
    unexpected = next(d for d in body["discrepancies"] if d["kind"] == "unexpected_in_target")
    assert unexpected["entity"] == "organization"
    assert unexpected["count"] == 2
    assert "2 of them the run recorded as failed" in unexpected["explanation"]
    failed = {c["check"] for c in body["checks"] if not c["ok"]}
    assert failed == {"target_count", "target_ids"}


def test_a_recheck_after_the_namespace_changed_finds_what_is_missing(client, ready) -> None:
    run = dry_run(client, ready, limit_per_entity=5)
    # somebody throws the staging records away after the run
    purged = client.delete(f"/target/v1/namespaces/{run['namespace']}", headers=auth())
    assert purged.status_code == 200

    recheck = client.post(f"/api/v1/projects/{ready.id}/migrations/{run['id']}/reconcile")
    assert recheck.status_code == 201
    body = recheck.json()
    assert body["trigger"] == "recheck"
    assert body["status"] == "discrepancies"
    missing = {d["entity"]: d for d in body["discrepancies"] if d["kind"] == "missing_in_target"}
    assert missing["organization"]["count"] == 5
    assert len(missing["organization"]["sample_ids"]) == 5

    history = client.get(
        f"/api/v1/projects/{ready.id}/migrations/{run['id']}/reconciliations"
    ).json()
    assert [h["trigger"] for h in history] == ["recheck", "dry_run"]
    # the latest is the one a report reads
    latest = client.get(f"/api/v1/projects/{ready.id}/migrations/{run['id']}/reconciliation").json()
    assert latest["id"] == body["id"]


def test_a_recheck_of_an_unchanged_namespace_still_balances(client, ready) -> None:
    run = dry_run(client, ready, limit_per_entity=5)
    body = client.post(f"/api/v1/projects/{ready.id}/migrations/{run['id']}/reconcile").json()
    assert body["status"] == "balanced"


def test_what_cannot_be_reconciled_says_why(client, ready) -> None:
    purged = dry_run(client, ready, limit_per_entity=3, purge_namespace_after=True)
    # the run reconciled before it purged
    first = client.get(
        f"/api/v1/projects/{ready.id}/migrations/{purged['id']}/reconciliation"
    ).json()
    assert first["status"] == "balanced"
    assert first["target_counts"]["organization"] == 3
    refused = client.post(f"/api/v1/projects/{ready.id}/migrations/{purged['id']}/reconcile")
    assert refused.status_code == 409
    assert "purged" in refused.json()["error"]["message"]

    validation = client.post(f"/api/v1/projects/{ready.id}/validate", json={}).json()
    none = client.get(f"/api/v1/projects/{ready.id}/migrations/{validation['id']}/reconciliation")
    assert none.status_code == 404
    refused = client.post(f"/api/v1/projects/{ready.id}/migrations/{validation['id']}/reconcile")
    assert refused.status_code == 409


# --- the readiness report ------------------------------------------------------------------------


def test_with_nothing_stored_the_report_is_a_preview(client, ready) -> None:
    body = client.get(f"/api/v1/projects/{ready.id}/reports/readiness").json()
    assert body["stored"] is False
    assert body["id"] is None
    assert body["status"] == "BLOCKED"
    gates = {g["code"]: g for g in body["content"]["gates"]}
    assert gates["rehearsal"]["outcome"] == "blocker"
    assert gates["mappings_decided"]["outcome"] == "pass"
    assert client.get(f"/api/v1/projects/{ready.id}/reports").json() == []


def test_a_report_after_a_partial_rehearsal_is_blocked_and_says_so(client, ready) -> None:
    dry_run(client, ready, limit_per_entity=40)
    response = client.post(f"/api/v1/projects/{ready.id}/reports/readiness", json={})
    assert response.status_code == 201
    report = response.json()
    assert report["stored"] is True
    assert report["status"] == "BLOCKED"
    assert report["summary_origin"] == "template"  # no model in the test environment
    content = report["content"]
    blockers = {g["code"] for g in content["blockers"]}
    assert "rehearsal" in blockers
    assert content["facts"]["reconciliation"]["status"] == "balanced"
    # the sample's real problems become work, counted in records
    items = {i["code"]: i for i in content["work_items"]}
    assert items["contact:malformed_email"]["kind"] == "data_fix"
    assert items["contact:malformed_email"]["records"] == 106
    assert items["organization:missing_identifier"]["records"] == 13
    assert items["contact:parent_record_rejected"]["kind"] == "dependent"
    # personal data is counted, never quoted
    assert items["contact:malformed_email"]["values"] == []
    assert "@" not in json.dumps(content["work_items"])
    assert content["summary"]["note"].startswith("no model is configured")
    assert client.get(f"/api/v1/projects/{ready.id}").json()["stage"] == "reported"


def test_the_report_exports_as_markdown_and_json(client, ready) -> None:
    dry_run(client, ready, limit_per_entity=10)
    created = client.post(f"/api/v1/projects/{ready.id}/reports/readiness", json={}).json()

    md = client.get(f"/api/v1/projects/{ready.id}/reports/readiness?format=markdown")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert "attachment" in md.headers["content-disposition"]
    assert md.headers["content-disposition"].endswith(f'{created["id"][:8]}.md"')
    text = md.text
    assert text.startswith("# Implementation readiness: Apex Equipment Services")
    assert "**Status: BLOCKED**" in text
    assert "# Technical appendix" in text
    assert "—" not in text

    by_id = client.get(f"/api/v1/projects/{ready.id}/reports/{created['id']}").json()
    assert by_id["content"] == created["content"]
    listed = client.get(f"/api/v1/projects/{ready.id}/reports").json()
    assert listed[0]["id"] == created["id"]
    assert listed[0]["blockers"] >= 1


class SummaryFake:
    """a model that answers the summary prompt from the facts it is given. `invent` makes it
    quote a number that is not in the facts, every time."""

    name = "fake"
    model = "fake-writer-1"

    def __init__(self, invent: bool = False) -> None:
        self.invent = invent
        self.calls = 0

    def complete(self, *, system: str, user: str, output: type, max_tokens: int):
        self.calls += 1
        inputs = json.loads(_FENCE.search(user).group(1))
        facts = inputs["facts"]
        sent = facts["rehearsal"]["sent"]
        summary = (
            f"The rehearsal sent {sent} records to Meridian."
            if not self.invent
            else "The rehearsal sent 12,345 records to Meridian."
        )
        parsed = ReadinessSummary.model_validate(
            {
                "headline": f"{facts['customer']} is {facts['status']} for go-live.",
                "summary": summary,
                "blocker_explanations": [
                    {"code": code, "explanation": "The customer has to correct these records."}
                    for code in inputs["explain"]
                ],
            }
        )
        return StructuredResult(
            parsed=parsed,
            provider=self.name,
            model=self.model,
            latency_ms=2.0,
            input_tokens=900,
            output_tokens=200,
            request_id=f"req_summary_{self.calls}",
        )


def test_a_model_summary_is_used_when_it_keeps_to_the_facts_and_cached_after(client, ready) -> None:
    fake = SummaryFake()
    client.app.dependency_overrides[get_llm_provider] = lambda: fake
    dry_run(client, ready, limit_per_entity=10)
    first = client.post(f"/api/v1/projects/{ready.id}/reports/readiness", json={}).json()
    assert first["summary_origin"] == "model"
    summary = first["content"]["summary"]
    assert summary["model"] == "fake-writer-1"
    assert summary["summary"].startswith("The rehearsal sent")
    assert fake.calls == 1

    second = client.post(f"/api/v1/projects/{ready.id}/reports/readiness", json={}).json()
    assert second["summary_origin"] == "model"
    assert fake.calls == 1  # same facts, same prompt: served from the call log

    calls = client.get(f"/api/v1/projects/{ready.id}/llm-calls").json()
    summary_calls = [c for c in calls if c["purpose"] == "readiness_summary"]
    assert [c["cached"] for c in summary_calls] == [True, False]

    md = client.get(f"/api/v1/projects/{ready.id}/reports/readiness?format=markdown").text
    assert "Drafted by fake-writer-1 from the facts below and checked against them." in md


def test_a_model_summary_with_an_invented_number_falls_back_to_the_template(client, ready) -> None:
    fake = SummaryFake(invent=True)
    client.app.dependency_overrides[get_llm_provider] = lambda: fake
    dry_run(client, ready, limit_per_entity=10)
    report = client.post(f"/api/v1/projects/{ready.id}/reports/readiness", json={}).json()
    assert fake.calls == get_settings().llm_max_attempts
    assert report["summary_origin"] == "template"
    note = report["content"]["summary"]["note"]
    assert "failed the check against the facts" in note
    assert "12345" in note
    calls = client.get(f"/api/v1/projects/{ready.id}/llm-calls").json()
    failed = next(c for c in calls if c["purpose"] == "readiness_summary")
    assert failed["status"] == "error"
    assert failed["attempts"] == get_settings().llm_max_attempts


def test_model_drafting_can_be_turned_off(client, ready) -> None:
    fake = SummaryFake()
    client.app.dependency_overrides[get_llm_provider] = lambda: fake
    dry_run(client, ready, limit_per_entity=5)
    report = client.post(
        f"/api/v1/projects/{ready.id}/reports/readiness", json={"use_model": False}
    ).json()
    assert report["summary_origin"] == "template"
    assert fake.calls == 0


def test_a_report_on_an_unknown_project_is_a_404(client) -> None:
    missing = client.get("/api/v1/projects/00000000-0000-0000-0000-000000000001/reports/readiness")
    assert missing.status_code == 404
