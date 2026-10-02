"""the readiness decision, the work it implies, and the check on the model's summary.

Every function here is pure over a facts dict, so the facts below are written out by hand: one
clean customer, then the specific thing that changes the answer."""

from __future__ import annotations

import copy
import json

import pytest

from app.ai.schemas import BlockerExplanation, ReadinessSummary
from app.models.report import ReadinessStatus
from app.services import readiness as r

STATS = {
    "organization": {
        "source_rows": 100,
        "built": 100,
        "skipped_by_rule": 0,
        "valid": 100,
        "invalid": 0,
        "with_warnings": 0,
        "attempted": 100,
        "accepted": 100,
        "rejected": 0,
        "failed": 0,
        "blocked": 0,
        "retries": 0,
        "not_attempted": 0,
    },
    "subscription": {
        "source_rows": 60,
        "built": 60,
        "skipped_by_rule": 10,
        "valid": 50,
        "invalid": 0,
        "with_warnings": 0,
        "attempted": 50,
        "accepted": 50,
        "rejected": 0,
        "failed": 0,
        "blocked": 0,
        "retries": 0,
        "not_attempted": 0,
    },
}


def clean_facts() -> dict:
    totals = {
        k: sum(s[k] for s in STATS.values())
        for k in STATS["organization"]
        if k not in ("created", "unchanged")
    }
    return {
        "report_version": "1",
        "generated_at": "2026-10-02T08:00:00+00:00",
        "project": {
            "id": "00000000-0000-0000-0000-00000000000a",
            "customer": "Clean Co",
            "name": "Clean go-live",
            "target_platform": "Meridian",
            "target_environment": "staging",
            "source_systems": ["crm"],
            "stage": "dry_run_complete",
        },
        "data_profile": {
            "datasets": [
                {
                    "name": "organizations.csv",
                    "kind": "csv",
                    "rows": 100,
                    "columns": 5,
                    "profiled": True,
                    "errors": 0,
                    "warnings": 0,
                    "info": 1,
                }
            ],
            "rows": 100,
            "profiled": 1,
        },
        "mappings": {
            "total": 10,
            "approved": 8,
            "ignored": 2,
            "rejected": 0,
            "undecided": 0,
            "open_questions": 0,
            "approved_by_origin": {"model": 8},
            "approved_below_high_confidence": 0,
            "high_confidence_threshold": 0.85,
            "required_missing": [],
        },
        "open_questions": [],
        "dry_run": {
            "id": "11111111-2222-3333-4444-555555555555",
            "started_at": "2026-10-02T07:00:00+00:00",
            "duration_seconds": 1.5,
            "namespace": "99999999-0000-0000-0000-000000000000",
            "namespace_kept": True,
            "options": {"entities": None, "limit_per_entity": None},
            "partial": False,
            "config_version": "2026-10-01.1",
            "as_of": "2026-10-01",
            "plan_current": True,
            "plan_stale_reason": None,
            "stats": copy.deepcopy(STATS),
            "totals": totals,
            "issue_counts": {},
            "applied_rules": {},
            "target_counts": {"organization": 100, "subscription": 50},
            "issues_truncated": False,
            "failures_truncated": False,
        },
        "reconciliation": {
            "id": "r1",
            "status": "balanced",
            "trigger": "dry_run",
            "created_at": "2026-10-02T07:00:02+00:00",
            "entities": {},
            "totals": {},
            "checks_passed": 14,
            "checks_total": 14,
            "failed_checks": [],
            "discrepancies": [],
        },
        "issue_groups": [],
        "policy": {"min_entity_coverage": 0.95},
        "environment": {"target_fault_rate": 0.0},
    }


def gate(gates: list[dict], code: str) -> dict:
    return next(g for g in gates if g["code"] == code)


def content_for(facts: dict) -> dict:
    status, gates = r.assess(facts)
    items = r.work_items(facts)
    return {
        "status": status.value,
        "facts": facts,
        "gates": gates,
        "blockers": [g for g in gates if g["outcome"] == r.BLOCKER],
        "conditions": [g for g in gates if g["outcome"] == r.CONDITION],
        "work_items": items,
        "customer_questions": r.customer_questions(facts, items),
        "next_steps": r.next_steps(facts, status, gates, items),
        "technical_risks": r.technical_risks(facts),
        "report_id": None,
    }


# --- the decision --------------------------------------------------------------------------------


def test_a_clean_rehearsal_that_reconciles_is_ready() -> None:
    status, gates = r.assess(clean_facts())
    assert status is ReadinessStatus.ready
    assert {g["outcome"] for g in gates} == {"pass"}
    assert gate(gates, "coverage:subscription")["detail"].startswith("50 of 50 subscriptions")


def test_warnings_make_it_ready_with_conditions() -> None:
    facts = clean_facts()
    facts["dry_run"]["totals"]["with_warnings"] = 3
    status, gates = r.assess(facts)
    assert status is ReadinessStatus.ready_with_conditions
    assert gate(gates, "warnings")["outcome"] == "condition"


def test_a_few_records_left_behind_is_a_condition_and_many_is_a_blocker() -> None:
    facts = clean_facts()
    stats = facts["dry_run"]["stats"]["organization"]
    stats["accepted"], stats["invalid"], stats["valid"] = 97, 3, 97
    status, gates = r.assess(facts)
    assert status is ReadinessStatus.ready_with_conditions
    assert "97 of 100 organizations" in gate(gates, "coverage:organization")["detail"]

    stats["accepted"], stats["invalid"], stats["valid"] = 90, 10, 90
    status, gates = r.assess(facts)
    assert status is ReadinessStatus.blocked
    blocked = gate(gates, "coverage:organization")
    assert blocked["outcome"] == "blocker"
    assert "90.0%" in blocked["detail"] and "95%" in blocked["detail"]


def test_records_skipped_by_the_customers_own_rules_are_not_counted_against_coverage() -> None:
    # 60 subscriptions, 10 skipped by rule 11, 50 landed: that is every one in scope
    status, gates = r.assess(clean_facts())
    assert gate(gates, "coverage:subscription")["outcome"] == "pass"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda f: f.__setitem__("dry_run", None), "rehearsal"),
        (lambda f: f["dry_run"].__setitem__("partial", True), "rehearsal"),
        (lambda f: f["dry_run"].__setitem__("plan_current", False), "rehearsal_current"),
        (lambda f: f.__setitem__("reconciliation", None), "reconciliation"),
        (lambda f: f["mappings"].__setitem__("undecided", 2), "mappings_decided"),
        (lambda f: f["mappings"].__setitem__("open_questions", 1), "mappings_decided"),
        (lambda f: f["dry_run"]["totals"].__setitem__("rejected", 1), "target_accepted"),
        (lambda f: f["dry_run"]["totals"].__setitem__("failed", 1), "target_accepted"),
    ],
)
def test_each_blocker_blocks(change, code) -> None:
    facts = clean_facts()
    change(facts)
    status, gates = r.assess(facts)
    assert status is ReadinessStatus.blocked
    assert gate(gates, code)["outcome"] == "blocker"


def test_a_reconciliation_with_discrepancies_blocks_and_says_which() -> None:
    facts = clean_facts()
    facts["reconciliation"].update(
        status="discrepancies",
        checks_passed=12,
        discrepancies=[{"kind": "unexpected_in_target"}, {"kind": "target_count"}],
    )
    status, gates = r.assess(facts)
    assert status is ReadinessStatus.blocked
    detail = gate(gates, "reconciliation")["detail"]
    assert "2 of 14 checks failed" in detail
    assert "unexpected_in_target" in detail


# --- the work ------------------------------------------------------------------------------------


def apex_like_groups() -> list[dict]:
    return [
        {
            "entity": "activity",
            "severity": "error",
            "error_type": "unmapped_value",
            "records": 215,
            "fields": ["kind"],
            "values": [["Call", 215]],
            "rule": None,
        },
        {
            "entity": "contact",
            "severity": "error",
            "error_type": "malformed_email",
            "records": 106,
            "fields": ["email"],
            "values": [],
            "rule": None,
        },
        {
            "entity": "contact",
            "severity": "error",
            "error_type": "parent_record_rejected",
            "records": 231,
            "fields": ["organization_id"],
            "values": [],
            "rule": None,
        },
        {
            "entity": "organization",
            "severity": "warning",
            "error_type": "date_in_future",
            "records": 3,
            "fields": ["customer_since"],
            "values": [],
            "rule": None,
        },
        {
            "entity": "subscription",
            "severity": "error",
            "error_type": "something_new",
            "records": 1,
            "fields": ["plan"],
            "values": [],
            "rule": None,
        },
    ]


def test_issues_become_work_with_an_owner() -> None:
    facts = clean_facts()
    facts["issue_groups"] = apex_like_groups()
    items = {i["code"]: i for i in r.work_items(facts)}

    call = items["activity:unmapped_value"]
    assert call["kind"] == "decision"
    assert call["owner"] == "customer decision"
    assert call["title"] == "215 activities carry kind values that no rule maps ('Call' (215))"
    assert "Which Meridian value should 'Call' in kind become?" in call["question"]

    email = items["contact:malformed_email"]
    assert email["kind"] == "data_fix"
    assert email["question"] is None

    waiting = items["contact:parent_record_rejected"]
    assert waiting["kind"] == "dependent"
    assert "fixing the account releases them" in waiting["title"]

    # one record reads as one record
    single = r.work_items(
        {
            "issue_groups": [
                {
                    "entity": "subscription",
                    "severity": "error",
                    "error_type": "undecidable_plan",
                    "records": 1,
                    "fields": ["plan"],
                    "values": [["Legacy Gold", 1]],
                    "rule": "legacy_gold_seat_split (customer rule 7)",
                }
            ]
        }
    )[0]
    assert single["question"].startswith("Which plan should this 1 subscription get?")
    assert "'Legacy Gold'" in single["question"] and "(1)" not in single["question"]

    # a warning is review work and does not block records
    assert items["organization:date_in_future"]["blocks_records"] is False
    # an issue type with no catalog entry still becomes work, with a plain title
    assert items["subscription:something_new"]["title"] == "1 subscription: something_new on plan"


def test_decisions_come_before_corrections_and_warnings_come_last() -> None:
    facts = clean_facts()
    facts["issue_groups"] = apex_like_groups()
    kinds = [i["kind"] for i in r.work_items(facts)]
    assert kinds == ["decision", "data_fix", "data_fix", "dependent", "review"]


def test_customer_questions_are_the_open_mapping_questions_and_the_decisions() -> None:
    facts = clean_facts()
    facts["issue_groups"] = apex_like_groups()
    facts["open_questions"] = [
        {
            "id": "abcdef12-0000",
            "dataset": "organizations.csv",
            "source_field": "tier",
            "question": "Is tier the plan?",
        }
    ]
    questions = r.customer_questions(facts, r.work_items(facts))
    assert [q["source"] for q in questions] == ["mapping review", "validation"]
    assert questions[0]["question"] == "Is tier the plan?"
    assert questions[1]["records"] == 215


def test_next_steps_follow_from_the_gates_and_the_work() -> None:
    facts = clean_facts()
    facts["issue_groups"] = apex_like_groups()
    content = content_for(facts)
    steps = " ".join(content["next_steps"])
    assert "Send the customer the 1 questions" in steps
    assert "record lists for 2 data corrections (107 records)" in steps
    assert "up to 231 more records" in steps
    assert "Purge staging namespace" in steps


def test_a_ready_customer_is_told_to_schedule_the_window() -> None:
    content = content_for(clean_facts())
    assert content["status"] == "READY"
    assert any("Schedule the production migration" in s for s in content["next_steps"])


def test_risks_include_the_projection_labelled_as_one() -> None:
    risks = {x["code"]: x for x in r.technical_risks(clean_facts())}
    assert "projection" in risks["sequential_writes"]["detail"]
    assert "staging_copy" in risks


# --- the summary ---------------------------------------------------------------------------------


def blocked_content() -> dict:
    facts = clean_facts()
    facts["issue_groups"] = apex_like_groups()
    stats = facts["dry_run"]["stats"]["organization"]
    stats["accepted"], stats["invalid"], stats["valid"] = 80, 20, 80
    facts["dry_run"]["totals"]["accepted"] = 130
    return content_for(facts)


def good_summary(content: dict) -> ReadinessSummary:
    codes = r._explain_codes(content["work_items"])
    return ReadinessSummary(
        headline=f"Clean Co is {content['status']} for go-live.",
        summary="The rehearsal sent 150 records and the target accepted 130 of them.",
        blocker_explanations=[
            BlockerExplanation(code=c, explanation="The customer has to correct these.")
            for c in codes
        ],
    )


def test_a_summary_that_keeps_to_the_facts_passes() -> None:
    content = blocked_content()
    facts = r.summary_facts(content)
    codes = r._explain_codes(content["work_items"])
    assert (
        r.check_summary(good_summary(content), facts=facts, status=content["status"], codes=codes)
        == []
    )


def test_an_invented_number_is_sent_back() -> None:
    content = blocked_content()
    facts = r.summary_facts(content)
    codes = r._explain_codes(content["work_items"])
    summary = good_summary(content)
    summary.summary = "About 87% of records landed and 1,234 still need work."
    problems = r.check_summary(summary, facts=facts, status=content["status"], codes=codes)
    assert len(problems) == 1
    assert "87" in problems[0] and "1234" in problems[0]


def test_another_status_word_is_sent_back() -> None:
    content = blocked_content()
    facts = r.summary_facts(content)
    codes = r._explain_codes(content["work_items"])
    summary = good_summary(content)
    summary.summary += " With two fixes the customer would be READY."
    problems = r.check_summary(summary, facts=facts, status=content["status"], codes=codes)
    assert any("'READY' is not this report's status" in p for p in problems)


def test_the_headline_has_to_state_the_status() -> None:
    content = blocked_content()
    summary = good_summary(content)
    summary.headline = "Clean Co is nearly there."
    problems = r.check_summary(
        summary,
        facts=r.summary_facts(content),
        status=content["status"],
        codes=r._explain_codes(content["work_items"]),
    )
    assert any("headline must state the status" in p for p in problems)


def test_explanations_must_cover_exactly_the_work_asked_about() -> None:
    content = blocked_content()
    summary = good_summary(content)
    summary.blocker_explanations = [BlockerExplanation(code="contact:made_up", explanation="x")]
    problems = r.check_summary(
        summary,
        facts=r.summary_facts(content),
        status=content["status"],
        codes=r._explain_codes(content["work_items"]),
    )
    assert any(p.startswith("missing blocker explanations") for p in problems)
    assert any("contact:made_up" in p for p in problems)


@pytest.mark.parametrize("make", [clean_facts, lambda: blocked_content()["facts"]])
def test_the_template_never_says_a_number_the_model_would_not_be_allowed_to(make) -> None:
    content = content_for(make())
    template = r.template_summary(content)
    summary = ReadinessSummary.model_validate(template)
    problems = r.check_summary(
        summary,
        facts=r.summary_facts(content),
        status=content["status"],
        codes=r._explain_codes(content["work_items"]),
    )
    assert problems == []


def test_the_markdown_has_the_executive_part_and_the_appendix() -> None:
    content = blocked_content()
    content["summary"] = {
        **r.template_summary(content),
        "origin": "template",
        "model": None,
        "note": None,
    }
    text = r.render_markdown(content)
    for heading in (
        "# Implementation readiness: Clean Co",
        "**Status: BLOCKED**",
        "## Executive summary",
        "## Blockers",
        "## Conditions",
        "## Work before go-live",
        "## Customer questions",
        "## Next steps",
        "## Technical risks",
        "# Technical appendix",
        "## Readiness gates",
        "## Data profile",
        "## Mappings",
        "## Validation",
        "## Migration dry run",
        "## Reconciliation",
        "## Provenance",
    ):
        assert heading in text, heading
    assert "—" not in text
    assert "No number was produced by a model." in text


def test_the_json_export_is_plain_json() -> None:
    content = blocked_content()
    content["summary"] = {
        **r.template_summary(content),
        "origin": "template",
        "model": None,
        "note": None,
    }
    assert json.loads(json.dumps(content))["status"] == "BLOCKED"


def test_the_dormant_account_conflict_gets_its_own_question() -> None:
    items = r.work_items(
        {
            "issue_groups": [
                {
                    "entity": "subscription",
                    "severity": "error",
                    "error_type": "business_rule_violation",
                    "records": 311,
                    "fields": ["status"],
                    "values": [],
                    "rule": "platform rule: no live subscription under a dormant account",
                }
            ]
        }
    )
    question = items[0]["question"]
    assert question.startswith("These 311 subscriptions are live in billing")
    assert "should it stay active in Meridian, or should the subscription end" in question


def test_the_model_explains_the_largest_actionable_blockers() -> None:
    facts = clean_facts()
    facts["issue_groups"] = apex_like_groups()
    codes = r._explain_codes(r.work_items(facts))
    # dependent records are released by other fixes and warnings block nothing
    assert codes == [
        "activity:unmapped_value",
        "contact:malformed_email",
        "subscription:something_new",
    ]
