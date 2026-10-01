"""the normalizers, one value at a time. every case here comes from the customer's real export."""

from __future__ import annotations

import pytest

from app.services.transform.config import load_config
from app.services.transform.converters import NOTHING, ConverterContext, apply_chain

BASE = ("trim", "blank_to_null")


@pytest.fixture(scope="module")
def config():
    return load_config()


def convert(config, target_path: str, converters: tuple[str, ...], value):
    ctx = ConverterContext(
        config=config,
        target_path=target_path,
        target_type="string",
        value_map=config.value_map(target_path),
    )
    return apply_chain(value, BASE + converters, ctx)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-08-26", "2024-08-26"),
        ("2020/03/28", "2020-03-28"),
        ("09/27/2022", "2022-09-27"),
        ("06/20/22", "2022-06-20"),
        ("09-Feb-2022", "2022-02-09"),
        ("December 14, 2019", "2019-12-14"),
        ("  2024-08-26  ", "2024-08-26"),
    ],
)
def test_the_six_date_formats_in_the_export(config, raw, expected) -> None:
    assert convert(config, "organization.customer_since", ("date",), raw).value == expected


def test_a_date_in_no_configured_format_is_reported_not_guessed(config) -> None:
    result = convert(config, "organization.customer_since", ("date",), "26th of August")
    assert result.failed
    assert result.error_type == "unparseable_date"
    assert "26th of August" in str(result.error)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-08-26T09:30:00Z", "2024-08-26T09:30:00Z"),
        ("2024-08-26 09:30:00-07:00", "2024-08-26T16:30:00Z"),
        ("09/27/2022 14:00", "2022-09-27T14:00:00Z"),
        ("14-May-2025 03:15 PM", "2025-05-14T15:15:00Z"),
    ],
)
def test_timestamps_are_normalized_to_utc(config, raw, expected) -> None:
    assert convert(config, "activity.occurred_at", ("datetime",), raw).value == expected


def test_a_timestamp_without_a_zone_says_so(config) -> None:
    result = convert(config, "activity.occurred_at", ("datetime",), "09/27/2022 14:00")
    assert "assumed UTC" in str(result.note)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$7,050,000", "7050000.00"),
        ("38600000", "38600000.00"),
        ("USD 60", "60.00"),
        ("$40,000.00", "40000.00"),
        ("12.35M", "12350000.00"),
        (5100, "5100.00"),
        ("1475.0", "1475.00"),
    ],
)
def test_money_loses_its_decoration(config, raw, expected) -> None:
    assert convert(config, "subscription.mrr_usd", ("money",), raw).value == expected


@pytest.mark.parametrize("raw", ["n/a", "", "-", "unknown", "  "])
def test_the_customers_placeholders_become_absent(config, raw) -> None:
    result = convert(config, "subscription.mrr_usd", ("money",), raw)
    assert result.value is NOTHING
    assert not result.failed


def test_a_negative_amount_is_refused(config) -> None:
    result = convert(config, "subscription.mrr_usd", ("money",), "-100")
    assert result.error_type == "out_of_range"


@pytest.mark.parametrize(
    ("raw", "expected"), [("1200", 1200), ("1200.0", 1200), ("  1200 ", 1200), ("1,200", 1200)]
)
def test_counts_stored_as_text_become_numbers(config, raw, expected) -> None:
    assert convert(config, "organization.employee_count", ("integer",), raw).value == expected


def test_a_fractional_count_is_not_a_count(config) -> None:
    result = convert(config, "organization.employee_count", ("integer",), "1200.7")
    assert result.error_type == "unparseable_number"


@pytest.mark.parametrize("raw", ["Y", "yes", "TRUE", "1", "t"])
def test_every_spelling_of_yes(config, raw) -> None:
    assert convert(config, "contact.is_primary", ("boolean",), raw).value is True


@pytest.mark.parametrize("raw", ["N", "no", "FALSE", "0", "f"])
def test_every_spelling_of_no(config, raw) -> None:
    assert convert(config, "contact.is_primary", ("boolean",), raw).value is False


def test_a_word_that_is_neither_is_reported(config) -> None:
    assert convert(config, "contact.is_primary", ("boolean",), "maybe").failed


def test_an_email_is_lowercased_but_never_repaired(config) -> None:
    good = convert(config, "contact.email", ("email",), "CORY.LOZANO@RIVERA.COM")
    assert good.value == "cory.lozano@rivera.com"

    # the export holds 139 of these. repairing them would send real mail to an invented address
    for broken in ("lindsey.herrera at wong.com", "matthew.franklin", "a@b"):
        result = convert(config, "contact.email", ("email",), broken)
        assert result.error_type == "malformed_email", broken
        assert result.value is NOTHING


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(699) 445-6029", "+16994456029"),
        ("757-204-6564", "+17572046564"),
        ("502.555.0113", "+15025550113"),
        ("6994456029", "+16994456029"),
        ("+1 415 555 2671", "+14155552671"),
        ("1-415-555-2671", "+14155552671"),
    ],
)
def test_phone_numbers_become_e164(config, raw, expected) -> None:
    assert convert(config, "contact.phone", ("phone_e164",), raw).value == expected


def test_a_number_too_short_to_complete_is_reported(config) -> None:
    result = convert(config, "contact.phone", ("phone_e164",), "757-2046")
    assert result.error_type == "phone_too_short"
    assert "7 digits" in str(result.error)


@pytest.mark.parametrize(
    ("raw", "first", "last"),
    [
        ("COLLINS, JILLIAN", "Jillian", "Collins"),
        ("Brandon Gonzales", "Brandon", "Gonzales"),
        ("Mrs. Dana Munoz MD", "Dana", "Munoz"),
        ("REYES-BRADLEY, ANN MARIE", "Ann Marie", "Reyes-Bradley"),
        ("DeLuca, Gina", "Gina", "DeLuca"),
        ("Dr. Steven Webb Jr.", "Steven", "Webb"),
    ],
)
def test_one_name_column_becomes_two(config, raw, first, last) -> None:
    result = convert(config, "contact.first_name", ("person_name_split",), raw)
    assert result.value == {"first_name": first, "last_name": last}


def test_a_name_with_no_surname_cannot_be_split(config) -> None:
    result = convert(config, "contact.first_name", ("person_name_split",), "Cher")
    assert result.error_type == "incomplete_name"


def test_a_website_gains_a_scheme(config) -> None:
    assert (
        convert(config, "organization.website", ("url",), "www.williams.com").value
        == "https://www.williams.com"
    )
    assert convert(config, "organization.website", ("url",), "N/A").value is NOTHING
    assert convert(config, "organization.website", ("url",), "no website").failed


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("US", "US"), ("USA", "US"), ("U.S.", "US"), ("United States", "US"), ("Canada", "CA")],
)
def test_country_words_become_iso_codes(config, raw, expected) -> None:
    assert convert(config, "organization.billing_country", ("country_code",), raw).value == expected


def test_an_unknown_country_is_reported(config) -> None:
    result = convert(config, "organization.billing_country", ("country_code",), "Freedonia")
    assert result.error_type == "unmapped_value"


def test_state_names_become_codes_and_unknown_regions_are_dropped(config) -> None:
    assert (
        convert(config, "organization.billing_region", ("region_code",), "Tennessee").value == "TN"
    )
    assert convert(config, "organization.billing_region", ("region_code",), "QC").value == "QC"
    unknown = convert(config, "organization.billing_region", ("region_code",), "Narnia")
    assert unknown.value is NOTHING
    assert not unknown.failed  # the region is optional detail, not grounds to refuse an account


@pytest.mark.parametrize(
    ("path", "raw", "expected"),
    [
        ("subscription.plan", "Legacy Gold", "enterprise"),
        ("subscription.plan", "Pro", "professional"),
        ("subscription.plan", "Basic", "starter"),
        ("subscription.plan", "ENT", "enterprise"),
        ("subscription.billing_cycle", "Q", "monthly"),
        ("subscription.billing_cycle", "yearly", "annual"),
        ("subscription.status", "paused", "past_due"),
        ("subscription.status", "expired", "cancelled"),
        ("subscription.status", "trialing", "trial"),
        ("organization.lifecycle_status", "On Hold", "active"),
        ("organization.lifecycle_status", "Closed", "churned"),
        ("organization.lifecycle_status", "ACTIVE", "active"),
        ("contact.role", "billing contact", "billing"),
        ("contact.role", "CTO", "technical"),
        ("activity.kind", "Support Ticket", "support_ticket"),
    ],
)
def test_the_customers_words_become_the_platforms(config, path, raw, expected) -> None:
    assert convert(config, path, ("enum_map",), raw).value == expected


def test_a_value_the_rules_do_not_cover_is_never_guessed(config) -> None:
    # `Trial` is a status in this export, not a plan, and no customer rule says which plan it is
    plan = convert(config, "subscription.plan", ("enum_map",), "Trial")
    assert plan.error_type == "unmapped_value"
    # `Actve` is a typo nothing resolves
    status = convert(config, "organization.lifecycle_status", ("enum_map",), "Actve")
    assert status.error_type == "unmapped_value"
    # `Call` has no home in Meridian's closed list of activity kinds
    kind = convert(config, "activity.kind", ("enum_map",), "Call")
    assert kind.error_type == "unmapped_value"


def test_an_unmapped_value_can_be_left_to_the_platforms_default(config) -> None:
    # the industry map says `default`, so an unknown code is a note rather than a rejection
    result = convert(config, "organization.industry", ("enum_map",), "31")
    assert result.value is NOTHING
    assert not result.failed
    assert "default" in str(result.note)


def test_a_mapped_value_says_which_customer_rule_moved_it(config) -> None:
    note = str(convert(config, "subscription.status", ("enum_map",), "paused").note)
    assert "customer rule 10" in note


def test_identifiers_keep_their_shape(config) -> None:
    assert (
        convert(config, "organization.organization_id", ("identifier",), " 10103 ").value == "10103"
    )
    assert convert(config, "contact.contact_id", ("identifier",), "CT-000102").value == "CT-000102"
    assert convert(config, "organization.organization_id", ("identifier",), "10 103").failed


def test_a_chain_stops_at_the_first_failure_and_keeps_the_notes(config) -> None:
    result = convert(config, "subscription.mrr_usd", ("money",), "  about $5k-ish  ")
    assert result.failed
    assert result.value is NOTHING
