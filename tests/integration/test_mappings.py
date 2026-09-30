"""the mapping stage over http: suggest with a fake model, cache hits, review decisions,
questions to the customer, manual mode, and what happens when the model misbehaves."""

import uuid

import pytest

from app.ai.provider import NullProvider, get_llm_provider
from app.core.http import get_http_client
from tests.fakes import FakeProvider, bad_target_responder

pytestmark = pytest.mark.integration

BILLING_URL = "http://testserver/mock/billing/v1/subscriptions"
RULES = "sample_customer/business_rules.md"


def _profiled_project(client) -> str:
    r = client.post(
        "/api/v1/projects",
        json={
            "customer_name": "Apex Equipment Services",
            "project_name": "go-live",
            "notes": "customer_tier is disputed internally",
        },
    )
    pid = r.json()["id"]
    for name, kind, location in (
        ("organizations.csv", "csv", "sample_customer/data/organizations.csv"),
        ("contacts.csv", "csv", "sample_customer/data/contacts.csv"),
        ("subscriptions (billing api)", "api", BILLING_URL),
    ):
        assert (
            client.post(
                f"/api/v1/projects/{pid}/sources",
                json={"name": name, "kind": kind, "location": location},
            ).status_code
            == 201
        )
    client.app.dependency_overrides[get_http_client] = lambda: client
    try:
        assert client.post(f"/api/v1/projects/{pid}/sources/profile").status_code == 200
    finally:
        client.app.dependency_overrides.pop(get_http_client, None)
    return pid


def _use(client, provider) -> None:
    client.app.dependency_overrides[get_llm_provider] = lambda: provider


def _mappings(client, pid: str) -> dict[tuple[str, str], dict]:
    r = client.get(f"/api/v1/projects/{pid}/mappings")
    assert r.status_code == 200, r.text
    return {(m["dataset_name"], m["source_field"]): m for m in r.json()}


def test_suggest_review_clarify_flow(client) -> None:
    pid = _profiled_project(client)
    fake = FakeProvider()
    _use(client, fake)
    try:
        r = client.post(f"/api/v1/projects/{pid}/mappings/suggest", json={"rules_document": RULES})
        assert r.status_code == 200, r.text
        run = r.json()
        assert run["provider"] == "fake" and run["model"] == "fake-mapper-1"
        assert run["stage"] == "mapped"
        assert len(run["datasets"]) == 3 and not any(d["cached"] for d in run["datasets"])
        assert all(d["origin"] == "model" for d in run["datasets"])
        assert run["counts"]["needs_clarification"] == 1
        assert len(fake.calls) == 3
        # the brief carried the rules and the customer, never a row
        assert "is an executive designation assigned by the VP of Sales" in fake.calls[0]["user"]
        assert "customer_tier is disputed" in fake.calls[0]["user"]

        by = _mappings(client, pid)
        acct = by[("organizations.csv", "acct_num")]
        assert acct["target_path"] == "organization.organization_id"
        assert acct["origin"] == "model" and acct["confidence"] == 0.97
        assert acct["status"] == "suggested"
        assert acct["comparison_class"] in ("UNMAPPED", "AMBIGUOUS")
        tier = by[("organizations.csv", "customer_tier")]
        assert tier["status"] == "needs_clarification" and tier["open_question_count"] == 1
        website = by[("organizations.csv", "website")]
        assert website["transformation_required"] is True
        assert website["transformation"] == "fake rule"
        assert by[("subscriptions (billing api)", "acct_num")]["target_path"] == (
            "subscription.organization_id"
        )

        # same brief again: served from the cache, no new model call
        again = client.post(
            f"/api/v1/projects/{pid}/mappings/suggest", json={"rules_document": RULES}
        ).json()
        assert all(d["cached"] for d in again["datasets"])
        assert len(fake.calls) == 3
        calls = client.get(f"/api/v1/projects/{pid}/llm-calls").json()
        assert len(calls) == 6
        assert sum(1 for c in calls if c["cached"]) == 3
        real = [c for c in calls if not c["cached"]]
        assert all(c["status"] == "ok" and c["input_tokens"] == 1000 for c in real)
        assert all(c["request_id"].startswith("req_fake_") for c in real)

        # a person decides
        r = client.patch(
            f"/api/v1/projects/{pid}/mappings/{acct['id']}",
            json={"action": "approve", "reviewer": "jane", "note": "account number is the id"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved" and r.json()["decided_by"] == "jane"
        assert client.get(f"/api/v1/projects/{pid}").json()["stage"] == "in_review"

        notes = by[("organizations.csv", "notes")]
        assert (
            client.patch(
                f"/api/v1/projects/{pid}/mappings/{notes['id']}", json={"action": "ignore"}
            ).json()["status"]
            == "ignored"
        )

        full_name = by[("contacts.csv", "full_name")]
        edited = client.patch(
            f"/api/v1/projects/{pid}/mappings/{full_name['id']}",
            json={
                "action": "edit",
                "target_path": "contact.first_name",
                "transformation": "split on the first space; 'LAST, FIRST' is reversed",
            },
        ).json()
        assert edited["status"] == "suggested" and edited["origin"] == "manual"
        assert edited["target_path"] == "contact.first_name"
        assert edited["transformation_required"] is True

        bad = client.patch(
            f"/api/v1/projects/{pid}/mappings/{full_name['id']}",
            json={"action": "approve", "target_path": "contact.nickname"},
        )
        assert bad.status_code == 422 and bad.json()["error"]["type"] == "unknown_target"
        owner = by[("organizations.csv", "account_owner")]
        no_target = client.patch(
            f"/api/v1/projects/{pid}/mappings/{owner['id']}", json={"action": "approve"}
        )
        assert no_target.status_code == 422
        assert no_target.json()["error"]["type"] == "target_required"

        # the floor holds: asking for 0.5 still approves at the configured 0.85
        bulk = client.post(
            f"/api/v1/projects/{pid}/mappings/bulk-approve", json={"min_confidence": 0.5}
        ).json()
        assert bulk["min_confidence"] == 0.85
        after = _mappings(client, pid)
        assert bulk["approved"] == sum(
            1
            for m in by.values()
            if m["status"] == "suggested"
            and m["target_path"]
            and m["confidence"] >= 0.85
            and m["id"] not in (acct["id"], full_name["id"])
        )
        assert after[("organizations.csv", "website")]["status"] == "approved"
        assert after[("contacts.csv", "full_name")]["status"] == "suggested"  # 0.6 stays

        # the customer answers and the answer resolves the mapping
        questions = client.get(f"/api/v1/projects/{pid}/clarifications?status=open").json()
        assert len(questions) == 1
        q = questions[0]
        assert q["source_field"] == "customer_tier" and q["origin"] == "model"
        assert q["context"]["values"]
        answered = client.patch(
            f"/api/v1/projects/{pid}/clarifications/{q['id']}",
            json={
                "answer": "priority, Strategic maps to strategic, Platinum is Gold",
                "answered_by": "ops lead",
                "resolution": {
                    "action": "approve",
                    "target_path": "organization.account_priority",
                    "transformation": "Gold and Platinum to priority, Strategic to strategic",
                },
            },
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["status"] == "answered"
        tier_after = client.get(f"/api/v1/projects/{pid}/mappings/{tier['id']}").json()
        assert tier_after["status"] == "approved"
        assert tier_after["target_path"] == "organization.account_priority"
        assert tier_after["origin"] == "manual" and tier_after["open_question_count"] == 0
        twice = client.patch(
            f"/api/v1/projects/{pid}/clarifications/{q['id']}", json={"answer": "again"}
        )
        assert twice.status_code == 409

        # a manual question, withdrawn
        created = client.post(
            f"/api/v1/projects/{pid}/clarifications",
            json={"mapping_id": full_name["id"], "question": "Is full_name always 'Last, First'?"},
        )
        assert created.status_code == 201
        assert (
            client.get(f"/api/v1/projects/{pid}/mappings/{full_name['id']}").json()["status"]
            == "needs_clarification"
        )
        assert (
            client.delete(
                f"/api/v1/projects/{pid}/clarifications/{created.json()['id']}"
            ).status_code
            == 204
        )
        assert (
            client.get(f"/api/v1/projects/{pid}/mappings/{full_name['id']}").json()["status"]
            == "suggested"
        )

        summary = client.get(f"/api/v1/projects/{pid}/mappings/summary").json()
        assert summary["total"] == len(by)
        assert summary["counts"]["approved"] >= 3 and summary["open_questions"] == 0
        assert summary["thresholds"] == {"high": 0.85, "low": 0.6}
        org_cov = next(c for c in summary["coverage"] if c["entity"] == "organization")
        assert "organization.organization_id" in org_cov["approved"]

        # re-suggesting keeps every decision
        kept = client.post(
            f"/api/v1/projects/{pid}/mappings/suggest", json={"rules_document": RULES}
        ).json()
        decided = sum(
            1 for m in after.values() if m["status"] in ("approved", "rejected", "ignored")
        )
        assert sum(d["kept"] for d in kept["datasets"]) >= decided
        assert _mappings(client, pid)[("organizations.csv", "acct_num")]["decided_by"] == "jane"

        # force starts over and re-asks
        forced = client.post(
            f"/api/v1/projects/{pid}/mappings/suggest",
            json={"rules_document": RULES, "force": True},
        ).json()
        assert sum(d["kept"] for d in forced["datasets"]) == 0
        assert not any(d["cached"] for d in forced["datasets"])
        fresh = _mappings(client, pid)
        assert fresh[("organizations.csv", "acct_num")]["status"] == "suggested"
        assert fresh[("organizations.csv", "customer_tier")]["open_question_count"] == 1
        assert client.get(f"/api/v1/projects/{pid}").json()["stage"] == "mapped"

        # deciding everything moves the project on
        for m in _mappings(client, pid).values():
            action = "approve" if m["target_path"] else "ignore"
            r = client.patch(f"/api/v1/projects/{pid}/mappings/{m['id']}", json={"action": action})
            assert r.status_code == 200, r.text
        assert client.get(f"/api/v1/projects/{pid}").json()["stage"] == "ready_to_transform"
        some = next(iter(_mappings(client, pid).values()))
        client.patch(f"/api/v1/projects/{pid}/mappings/{some['id']}", json={"action": "reopen"})
        assert client.get(f"/api/v1/projects/{pid}").json()["stage"] == "in_review"
    finally:
        client.app.dependency_overrides.pop(get_llm_provider, None)


def test_manual_mode_seeds_from_the_comparison(client) -> None:
    pid = _profiled_project(client)
    _use(client, NullProvider())
    try:
        r = client.post(f"/api/v1/projects/{pid}/mappings/suggest", json={})
        assert r.status_code == 200, r.text
        run = r.json()
        assert run["provider"] == "none" and run["model"] is None
        assert all(d["origin"] == "heuristic" for d in run["datasets"])
        assert client.get(f"/api/v1/projects/{pid}/llm-calls").json() == []
        by = _mappings(client, pid)
        assert all(m["confidence"] is None for m in by.values())
        assert by[("contacts.csv", "contact_mail")]["target_path"] == "contact.email"
        assert by[("organizations.csv", "acct_num")]["target_path"] is None
        assert by[("organizations.csv", "customer_tier")]["target_path"] is None
        asked = [m for m in by.values() if m["status"] == "needs_clarification"]
        assert asked and all(m["comparison_class"] == "AMBIGUOUS" for m in asked)
        questions = client.get(f"/api/v1/projects/{pid}/clarifications").json()
        assert len(questions) == len(asked)
        assert all(q["origin"] == "heuristic" for q in questions)
    finally:
        client.app.dependency_overrides.pop(get_llm_provider, None)


def test_suggest_needs_a_profile_and_a_known_dataset(client) -> None:
    pid = client.post(
        "/api/v1/projects", json={"customer_name": "Apex", "project_name": "x"}
    ).json()["id"]
    _use(client, FakeProvider())
    try:
        r = client.post(f"/api/v1/projects/{pid}/mappings/suggest", json={})
        assert r.status_code == 409 and r.json()["error"]["type"] == "invalid_state"
        pid = _profiled_project(client)
        r = client.post(f"/api/v1/projects/{pid}/mappings/suggest", json={"datasets": ["nope.csv"]})
        assert r.status_code == 404
        r = client.post(
            f"/api/v1/projects/{pid}/mappings/suggest",
            json={"rules_document": "../../etc/passwd"},
        )
        assert r.status_code == 422 and r.json()["error"]["type"] == "invalid_location"
        r = client.post(
            f"/api/v1/projects/{pid}/mappings/suggest",
            json={"rules_document": "sample_customer/data/organizations.csv"},
        )
        assert r.status_code == 422
        assert client.get(f"/api/v1/projects/{uuid.uuid4()}/mappings").status_code == 404
    finally:
        client.app.dependency_overrides.pop(get_llm_provider, None)


def test_model_that_keeps_breaking_the_contract_is_a_502_with_a_trail(client) -> None:
    pid = _profiled_project(client)
    fake = FakeProvider(bad_target_responder)
    _use(client, fake)
    try:
        r = client.post(
            f"/api/v1/projects/{pid}/mappings/suggest", json={"datasets": ["organizations.csv"]}
        )
        assert r.status_code == 502, r.text
        err = r.json()["error"]
        assert err["type"] == "llm_error" and err["details"]["attempts"] == 3
        assert "organization.does_not_exist" in "".join(err["details"]["problems"])
        calls = client.get(f"/api/v1/projects/{pid}/llm-calls").json()
        assert len(calls) == 1 and calls[0]["status"] == "error" and calls[0]["attempts"] == 3
        assert _mappings(client, pid) == {}
        assert client.get(f"/api/v1/projects/{pid}").json()["stage"] == "profiled"
    finally:
        client.app.dependency_overrides.pop(get_llm_provider, None)


def test_one_bad_answer_is_corrected_and_recorded(client) -> None:
    pid = _profiled_project(client)
    fake = FakeProvider(fail_first=1)
    _use(client, fake)
    try:
        r = client.post(
            f"/api/v1/projects/{pid}/mappings/suggest", json={"datasets": ["contacts.csv"]}
        )
        assert r.status_code == 200, r.text
        calls = client.get(f"/api/v1/projects/{pid}/llm-calls").json()
        assert len(calls) == 1 and calls[0]["attempts"] == 2 and calls[0]["status"] == "ok"
        assert calls[0]["input_tokens"] == 2000
    finally:
        client.app.dependency_overrides.pop(get_llm_provider, None)


def test_documents_and_target_fields_are_listed(client) -> None:
    docs = {d["name"]: d for d in client.get("/api/v1/sources/documents").json()}
    assert "business_rules.md" in docs and "implementation_notes.md" in docs
    assert docs["business_rules.md"]["location"] == "sample_customer/business_rules.md"
    fields = client.get("/api/v1/target-fields").json()
    assert {f["path"] for f in fields} >= {"organization.organization_id", "subscription.plan"}
