"""the customer's rules that need more than one column, and the ones that need every record."""

from __future__ import annotations

import pytest

from app.services.profiling.types import Severity
from app.services.transform import record_rules as rules
from app.services.transform.config import load_config
from app.services.transform.record_rules import RuleContext
from app.services.transform.types import RecordDraft


@pytest.fixture(scope="module")
def config():
    return load_config()


def context(config, rule_id: str) -> RuleContext:
    settings = {**config.record_rules, **config.cross_dataset_rules}[rule_id]
    return RuleContext(
        rule_id=rule_id, config=config, params=settings.params, citation=settings.rule
    )


def organization(**payload) -> RecordDraft:
    draft = RecordDraft(entity="organization", dataset="organizations.csv", source_row=1)
    draft.payload = {"organization_id": "10103", "lifecycle_status": "active", **payload}
    return draft


def subscription(**payload) -> RecordDraft:
    draft = RecordDraft(entity="subscription", dataset="subscriptions.json", source_row=1)
    draft.payload = {
        "subscription_id": "SUB-1",
        "organization_id": "10103",
        "plan": "enterprise",
        "status": "active",
        "start_date": "2026-09-01",
        **payload,
    }
    return draft


def contact(**payload) -> RecordDraft:
    draft = RecordDraft(entity="contact", dataset="contacts.csv", source_row=1)
    draft.payload = {
        "contact_id": "CT-1",
        "organization_id": "10103",
        "email": "a@b.com",
        "is_primary": False,
        **payload,
    }
    return draft


# --- rule 5: CA is Canada, except when it is California -----------------------------------------


def test_ca_with_a_us_state_is_california(config) -> None:
    draft = organization(billing_country="CA", billing_region="CO")
    draft.sources["organization.billing_country"] = "country"
    draft.raw = {"country": "CA"}
    rules.ca_country_from_region(draft, context(config, "ca_country_from_region"))
    assert draft.payload["billing_country"] == "US"
    assert any("California" in note for note in draft.notes)


def test_ca_with_a_canadian_province_stays_canada(config) -> None:
    draft = organization(billing_country="CA", billing_region="AB")
    draft.sources["organization.billing_country"] = "country"
    draft.raw = {"country": "CA"}
    rules.ca_country_from_region(draft, context(config, "ca_country_from_region"))
    assert draft.payload["billing_country"] == "CA"


def test_ca_with_no_region_is_flagged_not_decided(config) -> None:
    draft = organization(billing_country="CA")
    draft.sources["organization.billing_country"] = "country"
    draft.raw = {"country": "CA"}
    rules.ca_country_from_region(draft, context(config, "ca_country_from_region"))
    assert draft.payload["billing_country"] == "CA"
    assert [i.error_type for i in draft.issues] == ["ambiguous_country"]
    assert draft.issues[0].severity is Severity.warning


def test_a_country_that_was_never_ca_is_left_alone(config) -> None:
    draft = organization(billing_country="US", billing_region="CO")
    draft.sources["organization.billing_country"] = "country"
    draft.raw = {"country": "United States"}
    rules.ca_country_from_region(draft, context(config, "ca_country_from_region"))
    assert draft.payload["billing_country"] == "US"
    assert not draft.issues


# --- rule 2: dormant accounts are inactive -------------------------------------------------------


def test_an_account_with_no_activity_for_18_months_migrates_as_inactive(config) -> None:
    draft = organization()
    draft.raw = {"last_activity_date": "2022-01-04"}
    rules.dormant_account_inactive(draft, context(config, "dormant_account_inactive"))
    assert draft.payload["lifecycle_status"] == "inactive"
    assert any("dormant_account_inactive" in rule for rule in draft.applied_rules)


def test_a_recently_active_account_keeps_its_status(config) -> None:
    draft = organization()
    draft.raw = {"last_activity_date": "2026-08-01"}
    rules.dormant_account_inactive(draft, context(config, "dormant_account_inactive"))
    assert draft.payload["lifecycle_status"] == "active"
    assert not draft.applied_rules


def test_dormancy_does_not_revive_or_overwrite_a_churned_account(config) -> None:
    draft = organization(lifecycle_status="churned")
    draft.raw = {"last_activity_date": "2010-01-01"}
    rules.dormant_account_inactive(draft, context(config, "dormant_account_inactive"))
    assert draft.payload["lifecycle_status"] == "churned"


def test_a_missing_activity_date_decides_nothing(config) -> None:
    draft = organization()
    draft.raw = {"last_activity_date": ""}
    rules.dormant_account_inactive(draft, context(config, "dormant_account_inactive"))
    assert draft.payload["lifecycle_status"] == "active"


# --- rule 7: Legacy Gold depends on the seat count -----------------------------------------------


def test_legacy_gold_under_ten_seats_is_professional(config) -> None:
    draft = subscription(seats=4)
    draft.sources["subscription.plan"] = "subscription_level"
    draft.raw = {"subscription_level": "Legacy Gold"}
    rules.legacy_gold_seat_split(draft, context(config, "legacy_gold_seat_split"))
    assert draft.payload["plan"] == "professional"


def test_legacy_gold_with_enough_seats_stays_enterprise(config) -> None:
    draft = subscription(seats=250)
    draft.sources["subscription.plan"] = "subscription_level"
    draft.raw = {"subscription_level": "Legacy Gold"}
    rules.legacy_gold_seat_split(draft, context(config, "legacy_gold_seat_split"))
    assert draft.payload["plan"] == "enterprise"
    assert not draft.applied_rules


def test_legacy_gold_with_no_seat_count_cannot_be_decided(config) -> None:
    draft = subscription()
    draft.sources["subscription.plan"] = "subscription_level"
    draft.raw = {"subscription_level": "Legacy Gold"}
    rules.legacy_gold_seat_split(draft, context(config, "legacy_gold_seat_split"))
    assert [i.error_type for i in draft.issues] == ["undecidable_plan"]


# --- rule 11: dead trials do not migrate ---------------------------------------------------------


def test_a_trial_older_than_ninety_days_is_skipped_with_its_reason(config) -> None:
    draft = subscription(status="trial", start_date="2017-11-17")
    rules.drop_dead_trials(draft, context(config, "drop_dead_trials"))
    assert draft.skipped
    assert "customer rule 11" in str(draft.skip_reason)
    assert not draft.valid


def test_a_young_trial_migrates(config) -> None:
    draft = subscription(status="trial", start_date="2026-09-20")
    rules.drop_dead_trials(draft, context(config, "drop_dead_trials"))
    assert not draft.skipped


def test_an_active_subscription_is_never_dropped_for_age(config) -> None:
    draft = subscription(status="active", start_date="2017-11-17")
    rules.drop_dead_trials(draft, context(config, "drop_dead_trials"))
    assert not draft.skipped


# --- rule 1: the account row names the primary contact -------------------------------------------


def test_the_account_email_flags_the_primary_contact(config) -> None:
    org = organization()
    org.context["contact.email"] = "chosen@apex.com"
    contacts = [
        contact(contact_id="CT-1", email="other@apex.com"),
        contact(contact_id="CT-2", email="chosen@apex.com"),
    ]
    counts = rules.primary_from_account_email(
        {"organization": [org], "contact": contacts}, context(config, "primary_from_account_email")
    )
    assert counts["flagged"] == 1
    assert contacts[1].payload["is_primary"] is True
    assert contacts[0].payload["is_primary"] is False


def test_an_account_that_already_has_a_primary_is_left_alone(config) -> None:
    org = organization()
    org.context["contact.email"] = "chosen@apex.com"
    contacts = [contact(contact_id="CT-1", is_primary=True)]
    counts = rules.primary_from_account_email(
        {"organization": [org], "contact": contacts}, context(config, "primary_from_account_email")
    )
    assert counts == {
        "flagged": 0,
        "already_primary": 1,
        "email_not_in_contacts": 0,
        "no_contacts": 0,
    }


def test_no_contact_is_invented_from_an_email_address(config) -> None:
    """Meridian requires a first and last name. An address alone cannot become a contact, so the
    account is flagged for the customer instead."""
    org = organization()
    org.context["contact.email"] = "nobody@apex.com"
    contacts = [contact(contact_id="CT-1", email="someone@apex.com")]
    counts = rules.primary_from_account_email(
        {"organization": [org], "contact": contacts}, context(config, "primary_from_account_email")
    )
    assert counts["email_not_in_contacts"] == 1
    assert len(contacts) == 1
    assert [i.error_type for i in org.issues] == ["primary_contact_not_found"]
    assert org.issues[0].severity is Severity.warning


def test_an_account_with_no_contacts_at_all_is_flagged(config) -> None:
    org = organization()
    org.context["contact.email"] = "nobody@apex.com"
    other = contact(contact_id="CT-9", organization_id="99999")
    counts = rules.primary_from_account_email(
        {"organization": [org], "contact": [other]}, context(config, "primary_from_account_email")
    )
    assert counts["no_contacts"] == 1
    assert [i.error_type for i in org.issues] == ["primary_contact_missing"]
