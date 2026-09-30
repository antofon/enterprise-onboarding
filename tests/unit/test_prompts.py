"""the model sees statistics and vocabulary, never people. and the cache key behaves."""

import json
import re
from pathlib import Path

import pytest

from app.ai.prompts import (
    SYSTEM_PROMPT,
    build_mapping_prompt,
    clarification_template,
    field_brief,
)
from app.services.comparison import compare, field_comparison_dict
from app.services.profiling import load_source, profile_frame
from app.target.catalog import target_field_catalog

DATA = Path("sample_customer/data")


@pytest.fixture(scope="module")
def profiles():
    return {
        name: profile_frame(load_source(kind, str(DATA / name)), name=name)
        for name, kind in (
            ("organizations.csv", "csv"),
            ("contacts.csv", "csv"),
            ("subscriptions.json", "json"),
        )
    }


@pytest.fixture(scope="module")
def bundle(profiles):
    report = compare(list(profiles.values()))
    orgs = profiles["organizations.csv"]
    return build_mapping_prompt(
        profile=orgs,
        entity=report.datasets["organizations.csv"],
        comparisons=field_comparison_dict(report)["organizations.csv"],
        catalog=target_field_catalog(),
        customer="Apex Equipment Services",
        rules_text=Path("sample_customer/business_rules.md").read_text(),
        project_notes="kickoff notes",
        provider="fake",
        model="fake-1",
    )


def test_emails_phones_and_names_are_never_sent(profiles) -> None:
    orgs = profiles["organizations.csv"]
    contacts = profiles["contacts.csv"]
    email = field_brief(orgs.field("primary_contact_email"))
    assert email["type"] == "email"
    assert "values" not in email and "examples" not in email
    assert email["shape"] == "local@domain"
    phone = field_brief(contacts.field("phone_number"))
    assert "values" not in phone and "examples" not in phone
    assert phone["shapes"]
    name = field_brief(contacts.field("full_name"))
    assert "values" not in name and "examples" not in name
    company = field_brief(orgs.field("company_name"))
    assert "values" not in company and "examples" not in company
    owner = field_brief(orgs.field("account_owner"))
    assert "values" not in owner and "examples" not in owner


def test_vocabulary_columns_send_their_values(profiles) -> None:
    orgs = profiles["organizations.csv"]
    subs = profiles["subscriptions.json"]
    tier = field_brief(orgs.field("customer_tier"))
    assert set(tier["values"]) >= {"Gold", "Silver", "Bronze", "Strategic"}
    plan = field_brief(subs.field("subscription_level"))
    assert "Legacy Gold" in plan["values"]
    status = field_brief(orgs.field("company_status"))
    assert "Actve" in status["values"]


def test_dates_and_numbers_send_formats_and_ranges(profiles) -> None:
    orgs = profiles["organizations.csv"]
    created = field_brief(orgs.field("created_on"))
    assert len(created["formats"]) == 6
    assert "range" in created
    revenue = field_brief(orgs.field("annual_revenue"))
    assert "range" in revenue and revenue["numeric_text_count"] > 0


def test_prompt_carries_rules_catalog_and_comparison(bundle) -> None:
    assert bundle.system == SYSTEM_PROMPT
    body = json.loads(bundle.user.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    assert body["customer"] == "Apex Equipment Services"
    assert "Strategic" in body["customer_business_rules"]
    paths = {f["path"] for fields in body["target_catalog"].values() for f in fields}
    assert "organization.organization_id" in paths and "subscription.mrr_usd" in paths
    tier = next(f for f in body["fields"] if f["name"] == "customer_tier")
    assert tier["deterministic_comparison"]["classification"] == "UNMAPPED"
    acct = next(f for f in body["fields"] if f["name"] == "acct_num")
    assert acct["deterministic_comparison"]["classification"] in ("UNMAPPED", "AMBIGUOUS")
    # no raw row ever: none of the sample's email addresses may appear, only the shape marker
    dumped = json.dumps(body["fields"])
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", dumped)
    assert "local@domain" in dumped


def test_hash_is_stable_and_model_specific(profiles) -> None:
    report = compare(list(profiles.values()))
    common = dict(
        profile=profiles["contacts.csv"],
        entity="contact",
        comparisons=field_comparison_dict(report)["contacts.csv"],
        catalog=target_field_catalog(),
        customer="Apex",
        rules_text=None,
        project_notes=None,
    )
    a = build_mapping_prompt(**common, provider="anthropic", model="claude-opus-5")
    b = build_mapping_prompt(**common, provider="anthropic", model="claude-opus-5")
    c = build_mapping_prompt(**common, provider="anthropic", model="claude-sonnet-5")
    d = build_mapping_prompt(
        **{**common, "rules_text": "rule 1"}, provider="anthropic", model="claude-opus-5"
    )
    assert a.input_hash == b.input_hash
    assert a.input_hash != c.input_hash
    assert a.input_hash != d.input_hash


def test_clarification_template_names_the_choices() -> None:
    q = clarification_template(
        "customer_tier",
        ["Gold", "Silver", "Strategic"],
        ["organization.account_priority", "subscription.plan"],
    )
    assert "customer_tier" in q and "Strategic" in q
    assert "organization.account_priority" in q and "subscription.plan" in q
    assert clarification_template("mystery", None, []).endswith("need to be migrated?")
