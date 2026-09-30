"""the deterministic comparison is judged on the committed sample: it must catch the renames
the synonym table covers, refuse to guess at the semantic ones, and read the profiler's stats
into the right compatibility notes."""

from pathlib import Path

import pytest

from app.services.comparison import (
    Classification,
    compare,
    compatibility,
    field_comparison_dict,
    infer_entity,
    name_score,
    tokens,
)
from app.services.profiling import load_source, profile_frame
from app.services.profiling.types import DatasetProfile, FieldProfile
from app.target.catalog import catalog_by_path

DATA = Path("sample_customer/data")


@pytest.fixture(scope="module")
def report():
    profiles = [
        profile_frame(load_source(kind, str(DATA / name)), name=name)
        for name, kind in (
            ("organizations.csv", "csv"),
            ("contacts.csv", "csv"),
            ("subscriptions.json", "json"),
            ("activity.csv", "csv"),
        )
    ]
    return compare(profiles)


@pytest.fixture(scope="module")
def by_name(report):
    return field_comparison_dict(report)


def test_tokens_and_synonyms() -> None:
    assert tokens("acct_num") == ["organization", "number"]
    assert tokens("contactMail") == ["contact", "email"]
    assert tokens("seats") == ["seat"]
    assert tokens("billing_freq") == ["billing", "cycle"]
    assert tokens("renew_dt") == ["renewal", "date"]
    assert tokens("activity_type") == ["activity", "kind"]


def test_entity_is_read_from_the_dataset_name() -> None:
    assert infer_entity("organizations.csv") == "organization"
    assert infer_entity("accounts_export.csv") == "organization"
    assert infer_entity("contacts.csv") == "contact"
    assert infer_entity("subscriptions (billing api)") == "subscription"
    assert infer_entity("activity.csv") == "activity"
    assert infer_entity("misc.csv") is None


def test_name_score_shape() -> None:
    assert name_score(["email"], ["email"]) == 1.0
    assert name_score(["phone", "number"], ["phone"]) > 0.7
    assert name_score(["status"], ["lifecycle", "status"]) > 0.7
    # generic words in common are not enough on their own
    assert name_score(["full", "name"], ["first", "name"]) < 0.4
    assert name_score(["last", "touch"], ["last", "name"]) < 0.4
    assert name_score(["customer", "tier"], ["customer", "since"]) < 0.4
    assert name_score(["monthly", "amount"], ["mrr", "usd"]) == 0.0


def test_counts_cover_the_expected_classes(report) -> None:
    assert report.datasets == {
        "organizations.csv": "organization",
        "contacts.csv": "contact",
        "subscriptions.json": "subscription",
        "activity.csv": "activity",
    }
    assert report.counts["MATCHED"] >= 3
    assert report.counts["TRANSFORMATION_REQUIRED"] >= 15
    assert report.counts["AMBIGUOUS"] >= 2
    assert report.counts["UNMAPPED"] >= 8
    assert report.counts["INCOMPATIBLE"] == 0


def test_renames_the_synonym_table_can_see(by_name) -> None:
    orgs, contacts, subs, acts = (
        by_name["organizations.csv"],
        by_name["contacts.csv"],
        by_name["subscriptions.json"],
        by_name["activity.csv"],
    )
    assert orgs["company_name"].target == "organization.name"
    assert orgs["company_name"].classification == Classification.matched
    assert contacts["contact_ref"].target == "contact.contact_id"
    assert contacts["contact_mail"].target == "contact.email"
    assert subs["sub_id"].target == "subscription.subscription_id"
    assert subs["billing_freq"].target == "subscription.billing_cycle"
    assert subs["start_dt"].target == "subscription.start_date"
    assert subs["renew_dt"].target == "subscription.renewal_date"
    assert acts["activity_type"].target == "activity.kind"
    assert acts["description"].classification == Classification.matched


def test_transformation_required_carries_the_reason(by_name) -> None:
    orgs, contacts, subs = (
        by_name["organizations.csv"],
        by_name["contacts.csv"],
        by_name["subscriptions.json"],
    )
    status = orgs["company_status"]
    assert status.classification == Classification.transformation_required
    assert status.target == "organization.lifecycle_status"
    assert "On Hold" in status.reason and "Actve" in status.reason
    assert "mapping table" in orgs["industry_code"].reason
    assert "fail the target pattern" in orgs["country"].reason
    assert "http(s)://" in orgs["website"].reason
    assert "symbols" in orgs["annual_revenue"].reason
    assert "malformed emails" in contacts["contact_mail"].reason
    assert "E.164" in contacts["phone_number"].reason
    assert "spellings" in contacts["is_primary"].reason
    assert "date formats" in subs["start_dt"].reason
    assert "cast" in subs["seat_count"].reason


def test_semantic_mappings_are_left_alone(by_name) -> None:
    orgs, contacts, subs = (
        by_name["organizations.csv"],
        by_name["contacts.csv"],
        by_name["subscriptions.json"],
    )
    for fc in (
        orgs["acct_num"],
        orgs["customer_tier"],
        orgs["created_on"],
        orgs["last_activity_date"],
        contacts["full_name"],
        contacts["last_touch"],
        subs["subscription_level"],
        subs["monthly_amount"],
    ):
        assert fc.classification == Classification.unmapped, fc
        assert fc.target is None


def test_ambiguous_names_list_their_candidates(by_name) -> None:
    orgs = by_name["organizations.csv"]
    email = orgs["primary_contact_email"]
    assert email.classification == Classification.ambiguous
    assert email.candidates[0].target == "contact.email"
    assert email.candidates[0].in_entity is False
    state = orgs["hq_state"]
    assert state.classification == Classification.ambiguous
    assert state.candidates[0].target == "organization.billing_region"


def test_required_coverage_names_the_gaps(report) -> None:
    cov = {c.entity: c for c in report.coverage}
    assert cov["contact"].missing == [
        "contact.organization_id",
        "contact.first_name",
        "contact.last_name",
    ]
    assert cov["subscription"].missing == [
        "subscription.organization_id",
        "subscription.plan",
        "subscription.mrr_usd",
    ]
    assert cov["organization"].missing == ["organization.organization_id"]
    assert "organization.name" in cov["organization"].covered
    assert cov["activity"].missing == ["activity.organization_id"]


def test_incompatible_when_no_rule_helps() -> None:
    catalog = catalog_by_path()
    free_text = FieldProfile(
        name="seats",
        position=0,
        inferred_type="text",
        distinct_count=80,
        unique_pct=80,
        stats={"non_null_count": 100, "distinct_sample": ["a few", "lots"]},
    )
    assert compatibility(free_text, catalog["subscription.seats"])[0] == "incompatible"
    wide_status = FieldProfile(
        name="status", position=0, inferred_type="text", distinct_count=80, stats={}
    )
    assert compatibility(wide_status, catalog["subscription.status"])[0] == "incompatible"
    empty = FieldProfile(name="plan", position=0, inferred_type="empty", stats={})
    assert compatibility(empty, catalog["subscription.plan"])[0] == "incompatible"

    profile = DatasetProfile(
        name="subscriptions.csv", row_count=100, column_count=1, fields=[free_text]
    )
    fc = compare([profile]).fields[0]
    assert fc.classification == Classification.incompatible
    assert fc.target == "subscription.seats"


def test_entity_override_beats_the_guess() -> None:
    field = FieldProfile(
        name="status",
        position=0,
        inferred_type="category",
        stats={
            "non_null_count": 10,
            "value_counts": {"active": 8, "churned": 2},
        },
    )
    profile = DatasetProfile(name="export.csv", row_count=10, column_count=1, fields=[field])
    guessed = compare([profile]).fields[0]
    assert guessed.classification == Classification.ambiguous  # organization or subscription?
    told = compare([profile], entities={"export.csv": "organization"}).fields[0]
    assert told.target == "organization.lifecycle_status"
