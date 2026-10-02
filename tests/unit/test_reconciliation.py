"""reconciliation as a pure function: counts that add up balance, counts that do not are named,
and the target is compared identifier by identifier."""

from __future__ import annotations

from app.models.report import ReconciliationStatus
from app.services.reconciliation import EntityLedger, compare, first_errors
from app.services.transform.types import RecordDraft


def organizations(**overrides) -> EntityLedger:
    ledger = EntityLedger(
        entity="organization",
        source_rows=10,
        distinct_ids=9,
        built=10,
        skipped_by_rule=1,
        excluded=2,
        excluded_by_reason={"missing_required": 1, "duplicate_identifier": 1},
        valid=7,
        attempted=6,
        not_attempted=1,
        accepted=5,
        rejected=1,
        refusals_by_type={"business_rule_violation": 1},
        accepted_ids=["1", "2", "3", "4", "5"],
        refused_ids=["6"],
    )
    for key, value in overrides.items():
        setattr(ledger, key, value)
    return ledger


def test_a_run_whose_counts_add_up_and_whose_target_agrees_is_balanced() -> None:
    result = compare([organizations()], {"organization": {"1", "2", "3", "4", "5"}})
    assert result.status is ReconciliationStatus.balanced
    assert result.discrepancies == []
    assert all(c["ok"] for c in result.checks)
    names = {c["check"] for c in result.checks}
    assert names == {
        "rows_to_records",
        "records_accounted",
        "exclusions_explained",
        "valid_accounted",
        "sent_accounted",
        "target_count",
        "target_ids",
    }
    row = result.entities["organization"]
    assert row["in_scope"] == 9
    assert row["in_target"] == 5
    assert row["coverage"] == round(5 / 9, 4)
    assert result.totals["accepted"] == 5


def test_a_record_the_run_accepted_that_the_target_does_not_hold_is_named() -> None:
    result = compare([organizations()], {"organization": {"1", "2", "3", "4"}})
    assert result.status is ReconciliationStatus.discrepancies
    kinds = {d["kind"]: d for d in result.discrepancies}
    assert kinds["missing_in_target"]["count"] == 1
    assert kinds["missing_in_target"]["sample_ids"] == ["5"]


def test_a_failed_write_that_landed_is_found_and_explained() -> None:
    ledger = organizations(
        accepted=4,
        rejected=0,
        failed=2,
        accepted_ids=["1", "2", "3", "4"],
        refused_ids=["5", "6"],
    )
    result = compare([ledger], {"organization": {"1", "2", "3", "4", "5"}})
    unexpected = next(d for d in result.discrepancies if d["kind"] == "unexpected_in_target")
    assert unexpected["count"] == 1
    assert unexpected["sample_ids"] == ["5"]
    assert "1 of them the run recorded as failed" in unexpected["explanation"]


def test_a_count_that_does_not_add_up_is_a_discrepancy_in_the_run_itself() -> None:
    # one valid record that was neither sent, blocked nor counted as not reached
    result = compare([organizations(not_attempted=0)], {"organization": {"1", "2", "3", "4", "5"}})
    assert result.status is ReconciliationStatus.discrepancies
    failed = [c for c in result.checks if not c["ok"]]
    assert [c["check"] for c in failed] == ["valid_accounted"]
    assert result.discrepancies[0]["kind"] == "valid_accounted"
    assert result.discrepancies[0]["count"] == 1
    assert "defect in the run itself" in result.discrepancies[0]["explanation"]


def test_an_exclusion_without_a_reason_is_caught() -> None:
    result = compare(
        [organizations(excluded_by_reason={"missing_required": 1})],
        {"organization": {"1", "2", "3", "4", "5"}},
    )
    assert [c["check"] for c in result.checks if not c["ok"]] == ["exclusions_explained"]


def test_an_entity_the_run_never_wrote_balances_when_every_valid_record_is_not_reached() -> None:
    contacts = EntityLedger(entity="contact", source_rows=4, built=4, valid=4, not_attempted=4)
    result = compare([contacts], {"contact": set()})
    assert result.status is ReconciliationStatus.balanced


def test_the_reason_a_record_is_excluded_is_its_first_error() -> None:
    first = RecordDraft(entity="contact", dataset="contacts.csv", source_row=1)
    first.add_issue("malformed_email", "bad email")
    first.add_issue("parent_record_rejected", "parent")
    second = RecordDraft(entity="contact", dataset="contacts.csv", source_row=2)
    second.add_issue("parent_record_rejected", "parent")
    skipped = RecordDraft(entity="contact", dataset="contacts.csv", source_row=3)
    skipped.add_issue("missing_required", "x")
    skipped.skip("old trial", "rule 11")
    clean = RecordDraft(entity="contact", dataset="contacts.csv", source_row=4)
    assert first_errors([first, second, skipped, clean]) == {
        "malformed_email": 1,
        "parent_record_rejected": 1,
    }
