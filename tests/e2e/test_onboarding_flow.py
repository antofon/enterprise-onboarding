"""one customer, the whole way through, over http.

Create the project, attach four sources including the billing feed, profile them, compare the
schemas, propose mappings, review every column the way an implementation engineer would, validate,
rehearse the migration against the target platform, reconcile, and report readiness. No model is
configured, so the proposals come from the deterministic comparison: this test is about the
workflow holding together, and it has to hold together without an api key.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.http import get_http_client
from tests.golden import golden_targets

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

BILLING_FEED = "http://testserver/mock/billing/v1/subscriptions"
# the whole sample is 9,259 rows; the dry run writes a slice so the test stays quick. the stages
# before it all run over everything.
DRY_RUN_LIMIT = 25
# the billing feed is attached under its own name; the golden set keys it by the export file
GOLDEN_KEYS = {"subscriptions": "subscriptions.json"}


def test_a_customer_goes_from_four_source_files_to_a_readiness_report(client) -> None:
    client.app.dependency_overrides[get_http_client] = lambda: client
    try:
        # 1. the project
        project = client.post(
            "/api/v1/projects",
            json={
                "customer_name": "Apex Equipment Services",
                "project_name": "Apex go-live",
                "source_systems": ["Legacy CRM", "LegacyBill 4.2"],
            },
        )
        assert project.status_code == 201
        pid = project.json()["id"]
        assert project.json()["stage"] == "created"

        # 2. the sources: three files and one api
        available = {s["name"]: s for s in client.get("/api/v1/sources/available").json()}
        for name in ("organizations.csv", "contacts.csv", "activity.csv"):
            source = available[name]
            attached = client.post(
                f"/api/v1/projects/{pid}/sources",
                json={"name": name, "kind": source["kind"], "location": source["location"]},
            )
            assert attached.status_code == 201
        feed = client.post(
            f"/api/v1/projects/{pid}/sources",
            json={"name": "subscriptions", "kind": "api", "location": BILLING_FEED},
        )
        assert feed.status_code == 201

        # 3. profiling, including paging through the billing api
        profiled = client.post(f"/api/v1/projects/{pid}/sources/profile")
        assert profiled.status_code == 200
        rows = {d["name"]: d["row_count"] for d in profiled.json()["datasets"]}
        assert rows == {
            "activity.csv": 4947,
            "contacts.csv": 2279,
            "organizations.csv": 1030,
            "subscriptions": 1003,
        }
        assert client.get(f"/api/v1/projects/{pid}").json()["stage"] == "profiled"

        # 4. the deterministic comparison has already found the semantic gaps
        comparison = client.get(f"/api/v1/projects/{pid}/schema-comparison").json()
        assert comparison["datasets"]["organizations.csv"] == "organization"
        assert comparison["counts"]["UNMAPPED"] > 0

        # 5. proposals. no model is configured, so these come from the comparison
        suggested = client.post(f"/api/v1/projects/{pid}/mappings/suggest", json={})
        assert suggested.status_code == 200
        assert {d["origin"] for d in suggested.json()["datasets"]} == {"heuristic"}
        assert client.get(f"/api/v1/projects/{pid}").json()["stage"] == "mapped"

        # 6. the review: a person decides every column
        decided = _review_every_column(client, pid)
        assert decided["approved"] == 33  # every column the golden set gives a home to
        assert decided["ignored"] > 0
        # the identifiers are among what the reviewer had to supply by hand
        assert decided["edited"] >= 4
        summary = client.get(f"/api/v1/projects/{pid}/mappings/summary").json()
        assert summary["counts"]["suggested"] == 0
        assert summary["open_questions"] == 0
        assert summary["stage"] == "ready_to_transform"

        # 7. the plan, before anything runs
        plan = client.get(f"/api/v1/projects/{pid}/transformation-plan").json()
        assert plan["entities"] == ["organization", "contact", "subscription", "activity"]

        # 8. validation over all 9,259 rows
        validation = client.post(f"/api/v1/projects/{pid}/validate", json={}).json()
        assert validation["status"] == "completed"
        assert validation["totals"]["built"] == 9259
        assert validation["totals"]["valid"] > 0
        assert validation["totals"]["invalid"] > 0
        assert client.get(f"/api/v1/projects/{pid}").json()["stage"] == "validated"

        # 9. the rehearsal against the target platform
        dry_run = client.post(
            f"/api/v1/projects/{pid}/migrations/dry-run",
            json={"limit_per_entity": DRY_RUN_LIMIT},
        ).json()
        assert dry_run["status"] == "completed"
        organizations = dry_run["stats"]["organization"]
        assert organizations["attempted"] == DRY_RUN_LIMIT
        assert organizations["accepted"] == DRY_RUN_LIMIT
        assert organizations["rejected"] == 0

        # what the platform holds in this run's namespace is what the run says it wrote
        for entity, counts in dry_run["stats"].items():
            assert dry_run["target_counts"][entity] == counts.get("accepted", 0), entity

        # the live platform data was never touched
        auth = {"Authorization": f"Bearer {get_settings().target_api_token}"}
        live = client.get("/target/v1/counts", headers=auth).json()["counts"]
        assert live == {"organization": 0, "contact": 0, "subscription": 0, "activity": 0}

        assert client.get(f"/api/v1/projects/{pid}").json()["stage"] == "dry_run_complete"

        # 10. every stage left a trail somebody can read
        runs = client.get(f"/api/v1/projects/{pid}/migrations").json()
        assert [r["kind"] for r in runs] == ["dry_run", "validation"]
        issues = client.get(
            f"/api/v1/projects/{pid}/migrations/{validation['id']}/issue-breakdown"
        ).json()
        assert issues
        assert sum(row["count"] for row in issues) == sum(validation["issue_counts"].values())

        # 11. reconciliation: the run reconciled itself against the target as it finished
        rec = client.get(f"/api/v1/projects/{pid}/migrations/{dry_run['id']}/reconciliation").json()
        assert rec["status"] == "balanced", rec["discrepancies"]
        assert rec["totals"]["source_rows"] == 9259
        assert rec["totals"]["accepted"] == rec["totals"]["in_target"]

        # 12. the readiness report: a slice is not a rehearsal of the whole migration, and the
        # sample's real problems keep it blocked either way
        report = client.post(f"/api/v1/projects/{pid}/reports/readiness", json={})
        assert report.status_code == 201
        body = report.json()
        assert body["status"] == "BLOCKED"
        assert "rehearsal" in {g["code"] for g in body["content"]["blockers"]}
        assert body["content"]["customer_questions"]
        assert body["content"]["next_steps"]
        markdown = client.get(f"/api/v1/projects/{pid}/reports/readiness?format=markdown").text
        assert "**Status: BLOCKED**" in markdown
        assert client.get(f"/api/v1/projects/{pid}").json()["stage"] == "reported"
    finally:
        client.app.dependency_overrides.clear()


def _review_every_column(client, pid: str) -> dict[str, int]:
    """what an implementation engineer does at the review screen, without a model's help.

    The deterministic comparison proposes the columns whose names give them away. It cannot see
    that `acct_num` is the organization's identifier or that `full_name` splits into two target
    fields, which is what the mapping model is for. Here the reviewer supplies that knowledge by
    hand, from the golden answers, through the same edit action the workbench uses.
    """
    counts = {"approved": 0, "ignored": 0, "edited": 0, "answered": 0}
    bulk = client.post(
        f"/api/v1/projects/{pid}/mappings/bulk-approve", json={"reviewer": "e2e reviewer"}
    )
    assert bulk.status_code == 200
    counts["approved"] += bulk.json()["approved"]

    for question in client.get(f"/api/v1/projects/{pid}/clarifications").json():
        if question["status"] != "open":
            continue
        answered = client.patch(
            f"/api/v1/projects/{pid}/clarifications/{question['id']}",
            json={
                "answer": "Treat it as the account priority, not the plan.",
                "answered_by": "Apex operations lead",
            },
        )
        assert answered.status_code == 200, answered.text
        counts["answered"] += 1

    for mapping in client.get(f"/api/v1/projects/{pid}/mappings").json():
        if mapping["status"] in ("approved", "rejected", "ignored"):
            continue
        dataset = GOLDEN_KEYS.get(mapping["dataset_name"], mapping["dataset_name"])
        wanted = golden_targets(dataset).get(mapping["source_field"])
        url = f"/api/v1/projects/{pid}/mappings/{mapping['id']}"
        if wanted is None:
            decision = client.patch(
                url, json={"action": "ignore", "reviewer": "e2e reviewer", "note": "no home"}
            )
            assert decision.status_code == 200, decision.text
            counts["ignored"] += 1
            continue
        if mapping["target_path"] != wanted:
            edited = client.patch(
                url, json={"action": "edit", "target_path": wanted, "reviewer": "e2e reviewer"}
            )
            assert edited.status_code == 200, edited.text
            counts["edited"] += 1
        approved = client.patch(url, json={"action": "approve", "reviewer": "e2e reviewer"})
        assert approved.status_code == 200, approved.text
        counts["approved"] += 1
    return counts
