"""the profiler is checked against the synthetic generator's own defect counts. the generator
counts every defect it injects into manifest.json; where a defect maps one to one onto a
profiler measurement, the numbers must agree exactly. where duplication copies defective rows
(a duplicated contact keeps its malformed email) the profiler must find at least as many."""

import json
from pathlib import Path

import pandas as pd
import pytest

from app.core.errors import AppError, NotFoundError
from app.services.profiling import (
    assess,
    infer_references,
    load_source,
    profile_frame,
    resolve_source_path,
)

DATA = Path("sample_customer/data")
MANIFEST = json.loads((DATA / "manifest.json").read_text())
DEFECTS = MANIFEST["defects"]


@pytest.fixture(scope="module")
def sample():
    frames = {}
    profiles = {}
    for name, kind in (
        ("organizations.csv", "csv"),
        ("contacts.csv", "csv"),
        ("activity.csv", "csv"),
        ("subscriptions.json", "json"),
    ):
        df = load_source(kind, str(DATA / name))
        frames[name] = df
        profiles[name] = profile_frame(df, name=name)
    refs = infer_references(frames, profiles)
    for name, profile in profiles.items():
        profile.references = refs[name]
        profile.issues = assess(profile)
    return profiles


def test_row_counts_match_the_manifest(sample) -> None:
    for name, profile in sample.items():
        assert profile.row_count == MANIFEST["files"][name], name


def test_inferred_types_on_the_committed_sample(sample) -> None:
    orgs = sample["organizations.csv"]
    assert orgs.field("acct_num").inferred_type == "identifier"
    assert orgs.field("company_name").inferred_type == "text"
    assert orgs.field("company_status").inferred_type == "category"
    assert orgs.field("website").inferred_type == "url"
    assert orgs.field("created_on").inferred_type == "date"
    assert orgs.field("annual_revenue").inferred_type == "decimal"
    assert orgs.field("employee_count").inferred_type == "integer"
    assert orgs.field("primary_contact_email").inferred_type == "email"
    contacts = sample["contacts.csv"]
    assert contacts.field("contact_ref").inferred_type == "identifier"
    assert contacts.field("phone_number").inferred_type == "phone"
    assert contacts.field("is_primary").inferred_type == "boolean"
    assert sample["activity.csv"].field("occurred_at").inferred_type == "datetime"
    assert sample["subscriptions.json"].field("seat_count").inferred_type == "integer"


def test_key_columns_and_their_defects(sample) -> None:
    orgs = sample["organizations.csv"]
    assert orgs.key_column == "acct_num"
    assert orgs.key_missing == DEFECTS["org_missing_id"]
    assert orgs.key_duplicates == DEFECTS["org_duplicate_same_id"]
    assert sample["contacts.csv"].key_column == "contact_ref"
    assert sample["subscriptions.json"].key_column == "sub_id"
    assert sample["activity.csv"].key_column == "activity_id"


def test_defect_counts_are_found(sample) -> None:
    orgs = sample["organizations.csv"]
    contacts = sample["contacts.csv"]
    subs = sample["subscriptions.json"]

    emails_malformed = (
        orgs.field("primary_contact_email").stats["malformed_count"]
        + contacts.field("contact_mail").stats["malformed_count"]
    )
    assert emails_malformed >= DEFECTS["email_malformed"]
    # every duplicated contact keeps or upper-cases its email, so it repeats once normalized
    assert (
        contacts.field("contact_mail").stats["normalized_duplicates"]
        >= DEFECTS["contact_duplicate"]
    )
    assert contacts.field("phone_number").stats["malformed_count"] >= DEFECTS["phone_too_short"]
    assert orgs.field("website").stats["no_scheme_count"] >= DEFECTS["website_no_scheme"]
    assert orgs.field("website").stats["placeholder_count"] >= DEFECTS["website_placeholder"]
    assert subs.field("seat_count").null_count == DEFECTS["seats_missing"]
    assert contacts.field("is_primary").stats["variant_count"] >= DEFECTS["boolean_variant"] * 0.8
    assert subs.field("seat_count").stats["raw_types"]["str"] > 0  # ints and strings mixed


def test_date_formats_are_counted(sample) -> None:
    created = sample["organizations.csv"].field("created_on").stats
    assert len(created["format_counts"]) == 6
    assert 0 < created["iso_pct"] < 100
    occurred = sample["activity.csv"].field("occurred_at").stats
    assert "%Y-%m-%dT%H:%M:%SZ" in occurred["format_counts"]


def test_references_find_orphans(sample) -> None:
    contacts = sample["contacts.csv"]
    ref = contacts.references[0]
    assert ref.column == "acct_num" and ref.references == "organizations.csv.acct_num"
    # injected dangling accounts, plus the contacts of organizations whose id went missing
    assert ref.orphans >= DEFECTS["contact_dangling_account"]
    assert ref.resolved + ref.orphans == ref.checked
    subs = sample["subscriptions.json"].references[0]
    assert subs.orphans >= DEFECTS["sub_dangling_account"]
    assert sample["organizations.csv"].references == []


def test_quality_issues_carry_severity_and_counts(sample) -> None:
    orgs = sample["organizations.csv"]
    codes = {i.code for i in orgs.issues}
    assert {"key_missing", "key_duplicates", "malformed_email", "mixed_date_formats"} <= codes
    by_code = {i.code: i for i in orgs.issues}
    assert by_code["key_missing"].severity == "error"
    assert by_code["key_missing"].count == DEFECTS["org_missing_id"]
    assert by_code["mixed_date_formats"].severity == "warning"
    assert orgs.issues[0].severity == "error"  # sorted, errors first
    contacts = sample["contacts.csv"]
    assert {"orphan_references", "boolean_spellings"} <= {i.code for i in contacts.issues}


def test_small_frames_edge_cases() -> None:
    df = pd.DataFrame(
        {
            "count": ["0", "1", "1", "0"],
            "is_active": ["0", "1", "1", "0"],
            "flag": ["Y", "N", "yes", "no"],
            "empty": ["", "", "", ""],
            "zip": ["02134", "90210", "02134", "10001"],
        }
    )
    profile = profile_frame(df, name="edge.csv")
    assert profile.field("count").inferred_type == "integer"
    assert profile.field("is_active").inferred_type == "boolean"
    assert profile.field("flag").inferred_type == "boolean"
    assert profile.field("flag").stats["spelling_count"] == 4
    assert profile.field("empty").inferred_type == "empty"
    assert profile.field("empty").null_pct == 100.0
    assert profile.field("zip").sample_values == ["02134", "90210", "10001"]  # never a float
    assert profile.key_column is None
    assert profile.exact_duplicate_rows == 0
    assert any(i.code == "no_key_column" for i in assess(profile))


def test_source_paths_stay_inside_the_roots() -> None:
    assert resolve_source_path("sample_customer/data/organizations.csv").is_file()
    with pytest.raises(AppError):
        resolve_source_path("../../etc/passwd")
    with pytest.raises(AppError):
        resolve_source_path("/etc/passwd")
    with pytest.raises(AppError):
        resolve_source_path("pyproject.toml")
    with pytest.raises(NotFoundError):
        resolve_source_path("sample_customer/data/missing.csv")
