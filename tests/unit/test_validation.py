"""validation: the target's contract, the platform's invariants, and the customer's cross-checks.

Records are built by hand here so each rule can be put under pressure on its own. An error means
the record does not migrate; a warning means it does and somebody has to look.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.profiling.types import Severity
from app.services.transform.config import load_config
from app.services.transform.engine import TransformResult
from app.services.transform.plan import TransformationPlan
from app.services.transform.types import RecordDraft
from app.services.validation import validate

ORG = {
    "organization_id": "10103",
    "name": "Rivera, Garcia and Kirk",
    "lifecycle_status": "active",
    "industry": "energy",
    "billing_country": "US",
}
CONTACT = {
    "contact_id": "CT-1",
    "organization_id": "10103",
    "email": "jillian@apex.com",
    "first_name": "Jillian",
    "last_name": "Collins",
    "is_primary": True,
}
SUB = {
    "subscription_id": "SUB-1",
    "organization_id": "10103",
    "plan": "enterprise",
    "billing_cycle": "annual",
    "status": "active",
    "seats": 12,
    "start_date": "2024-01-01",
    "renewal_date": "2025-01-01",
    "mrr_usd": "5100.00",
}


@pytest.fixture(scope="module")
def config():
    return load_config()


def draft(entity: str, payload: dict, *, row: int = 1, raw: dict | None = None) -> RecordDraft:
    record = RecordDraft(entity=entity, dataset=f"{entity}s.csv", source_row=row, raw=raw or {})
    record.payload = dict(payload)
    return record


def result_for(**records: list[RecordDraft]) -> TransformResult:
    plan = TransformationPlan(
        customer="Apex Equipment Services",
        config_version="test",
        as_of=date(2026, 10, 1),
        entities=tuple(records),
    )
    return TransformResult(plan=plan, records=dict(records))


def issue_types(outcome, entity: str | None = None) -> list[str]:
    return [i.error_type for i in outcome.issues if entity is None or i.entity == entity]


def test_a_clean_batch_passes_with_nothing_to_report(config) -> None:
    outcome = validate(
        result_for(organization=[draft("organization", ORG)], contact=[draft("contact", CONTACT)]),
        config=config,
    )
    assert outcome.issues == []
    assert outcome.counts["organization"]["valid"] == 1
    assert outcome.counts["contact"]["valid"] == 1


def test_the_targets_own_contract_is_what_decides_a_field(config) -> None:
    missing_status = {k: v for k, v in ORG.items() if k != "lifecycle_status"}
    outcome = validate(
        result_for(
            organization=[
                draft("organization", missing_status),
                draft("organization", {**ORG, "organization_id": "2", "billing_country": "USA"}),
                draft("organization", {**ORG, "organization_id": "3", "website": "rivera"}),
            ]
        ),
        config=config,
    )
    # each error type comes from the target model's own complaint, not from a guess here
    assert issue_types(outcome) == ["missing_required", "value_too_long", "invalid_format"]
    assert outcome.counts["organization"]["valid"] == 0


def test_a_renewal_before_its_start_is_refused_by_the_model(config) -> None:
    outcome = validate(
        result_for(
            organization=[draft("organization", ORG)],
            subscription=[draft("subscription", {**SUB, "renewal_date": "2023-01-01"})],
        ),
        config=config,
    )
    assert "invalid_value" in issue_types(outcome, "subscription")


def test_the_same_identifier_twice_is_reported_on_the_second_record(config) -> None:
    first = draft("organization", ORG, row=4)
    second = draft("organization", {**ORG, "name": "Rivera Holdings"}, row=9)
    validate(result_for(organization=[first, second]), config=config)
    assert first.valid
    assert [i.error_type for i in second.issues] == ["duplicate_identifier"]
    assert "row 4" in second.issues[0].message


def test_a_child_whose_organization_is_absent_says_so(config) -> None:
    outcome = validate(
        result_for(
            organization=[draft("organization", ORG)],
            contact=[draft("contact", {**CONTACT, "organization_id": "99999"})],
        ),
        config=config,
    )
    assert issue_types(outcome, "contact") == ["missing_relationship"]
    assert "not in the customer's export" in outcome.issues[0].message


def test_a_child_whose_organization_cannot_migrate_is_told_to_fix_the_account(config) -> None:
    broken = draft("organization", {k: v for k, v in ORG.items() if k != "lifecycle_status"})
    outcome = validate(
        result_for(organization=[broken], contact=[draft("contact", CONTACT)]),
        config=config,
    )
    assert "parent_record_rejected" in issue_types(outcome, "contact")
    message = next(i.message for i in outcome.issues if i.entity == "contact")
    assert "missing_required" in message
    assert "Fixing the account releases it" in message


def test_only_one_contact_per_organization_can_be_the_primary(config) -> None:
    first = draft("contact", CONTACT)
    second = draft("contact", {**CONTACT, "contact_id": "CT-2", "email": "other@apex.com"})
    validate(
        result_for(organization=[draft("organization", ORG)], contact=[first, second]),
        config=config,
    )
    assert first.valid
    assert [i.error_type for i in second.issues] == ["duplicate_primary_contact"]


def test_an_organization_with_contacts_and_no_primary_is_a_warning(config) -> None:
    contacts = [draft("contact", {**CONTACT, "is_primary": False})]
    outcome = validate(
        result_for(organization=[draft("organization", ORG)], contact=contacts), config=config
    )
    assert issue_types(outcome, "contact") == ["no_primary_contact"]
    assert outcome.issues[0].severity is Severity.warning
    # a warning still migrates
    assert outcome.counts["contact"]["valid"] == 1
    assert outcome.counts["contact"]["with_warnings"] == 1


def test_one_address_per_organization(config) -> None:
    first = draft("contact", CONTACT)
    duplicate = draft("contact", {**CONTACT, "contact_id": "CT-2", "is_primary": False})
    validate(
        result_for(organization=[draft("organization", ORG)], contact=[first, duplicate]),
        config=config,
    )
    assert [i.error_type for i in duplicate.issues] == ["duplicate_contact_email"]


def test_a_dormant_account_cannot_hold_a_live_subscription(config) -> None:
    org = draft("organization", {**ORG, "lifecycle_status": "churned"})
    sub = draft("subscription", SUB)
    validate(result_for(organization=[org], subscription=[sub]), config=config)
    assert [i.error_type for i in sub.issues] == ["business_rule_violation"]
    assert "the CRM says the account is churned" in sub.issues[0].message


def test_when_the_dormancy_rule_caused_the_conflict_the_message_names_it(config) -> None:
    org = draft("organization", {**ORG, "lifecycle_status": "inactive"})
    org.applied("dormant_account_inactive (customer rule 2)")
    sub = draft("subscription", SUB)
    validate(result_for(organization=[org], subscription=[sub]), config=config)
    assert "dormant_account_inactive (customer rule 2)" in sub.issues[0].message


def test_a_date_after_the_migration_date_is_a_warning_not_a_rejection(config) -> None:
    sub = draft("subscription", {**SUB, "start_date": "2027-01-01", "renewal_date": "2028-01-01"})
    validate(
        result_for(organization=[draft("organization", ORG)], subscription=[sub]), config=config
    )
    assert [i.error_type for i in sub.issues] == ["date_in_future"]
    assert sub.valid


def test_the_strategic_tier_check_flags_the_conflict_and_changes_nothing(config) -> None:
    """Customer rule 4. The tier column has no approved target, so the plan is reported, not
    rewritten."""
    org = draft("organization", ORG, raw={"customer_tier": "Strategic"})
    sub = draft("subscription", {**SUB, "plan": "professional"})
    validate(result_for(organization=[org], subscription=[sub]), config=config)
    assert [i.error_type for i in sub.issues] == ["plan_conflicts_with_tier"]
    assert sub.issues[0].severity is Severity.warning
    assert sub.payload["plan"] == "professional"
    assert "customer rule 4" in str(sub.issues[0].rule)


def test_a_record_a_customer_rule_skipped_is_not_validated_at_all(config) -> None:
    skipped = draft("subscription", {"subscription_id": "SUB-9"})
    skipped.skip("dead trial", rule="drop_dead_trials (customer rule 11)")
    outcome = validate(
        result_for(organization=[draft("organization", ORG)], subscription=[skipped]), config=config
    )
    assert outcome.issues == []
    assert outcome.counts["subscription"] == {
        "built": 1,
        "skipped_by_rule": 1,
        "valid": 0,
        "invalid": 0,
        "with_warnings": 0,
    }


def test_a_record_that_already_failed_conversion_is_not_re_reported(config) -> None:
    broken = draft("contact", {**CONTACT, "email": None})
    broken.add_issue("malformed_email", "contact_mail: not an email", field_name="email")
    outcome = validate(
        result_for(organization=[draft("organization", ORG)], contact=[broken]), config=config
    )
    assert issue_types(outcome, "contact") == ["malformed_email"]
