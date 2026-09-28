import csv
import json
from pathlib import Path

from app.data.synthetic import generate


def _read(p: Path) -> list[dict]:
    with p.open() as f:
        return list(csv.DictReader(f))


def test_same_seed_same_bytes(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    generate(rows=60, seed=7, out_dir=a)
    generate(rows=60, seed=7, out_dir=b)
    for name in (
        "organizations.csv",
        "contacts.csv",
        "activity.csv",
        "subscriptions.json",
        "manifest.json",
    ):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_different_seed_different_data(tmp_path: Path) -> None:
    generate(rows=60, seed=1, out_dir=tmp_path / "a")
    generate(rows=60, seed=2, out_dir=tmp_path / "b")
    assert (tmp_path / "a/organizations.csv").read_bytes() != (
        tmp_path / "b/organizations.csv"
    ).read_bytes()


def test_shapes_and_manifest(tmp_path: Path) -> None:
    summary = generate(rows=300, seed=42, out_dir=tmp_path)
    orgs = _read(tmp_path / "organizations.csv")
    contacts = _read(tmp_path / "contacts.csv")
    subs = json.loads((tmp_path / "subscriptions.json").read_text())["data"]
    manifest = json.loads((tmp_path / "manifest.json").read_text())

    assert list(orgs[0].keys())[:4] == [
        "acct_num",
        "company_name",
        "company_status",
        "industry_code",
    ]
    assert len(orgs) > 300  # duplicates were added on top of the requested rows
    assert manifest["files"]["organizations.csv"] == len(orgs) == summary.files["organizations.csv"]
    assert manifest["files"]["contacts.csv"] == len(contacts)
    assert manifest["files"]["subscriptions.json"] == len(subs)
    assert manifest["seed"] == 42 and manifest["requested_rows"] == 300


def test_every_planned_defect_actually_shows_up(tmp_path: Path) -> None:
    summary = generate(rows=400, seed=3, out_dir=tmp_path)
    expected = {
        "date_non_iso",
        "status_unknown_enum",
        "status_case_variant",
        "state_full_name",
        "country_non_iso",
        "country_CA_ambiguous",
        "email_malformed",
        "phone_too_short",
        "org_duplicate_same_id",
        "org_duplicate_new_id",
        "org_missing_id",
        "contact_duplicate",
        "contact_dangling_account",
        "sub_dangling_account",
        "plan_name_nonstandard",
        "billing_freq_unknown_enum",
        "number_as_text",
        "number_with_symbols",
        "stray_whitespace",
        "boolean_variant",
        "name_last_first",
        "renewal_before_start",
        "rule_strategic_not_enterprise",
        "rule_active_but_dormant",
        "rule_inactive_org_active_sub",
    }
    missing = expected - {k for k, v in summary.defects.items() if v > 0}
    assert not missing, f"defects never injected: {sorted(missing)}"


def test_dangling_contacts_match_manifest(tmp_path: Path) -> None:
    summary = generate(rows=300, seed=11, out_dir=tmp_path)
    orgs = {r["acct_num"] for r in _read(tmp_path / "organizations.csv") if r["acct_num"]}
    contacts = _read(tmp_path / "contacts.csv")
    dangling = [c for c in contacts if c["acct_num"] and c["acct_num"] not in orgs]
    # duplicated contacts can copy a dangling row, so the file can hold a few more than injected
    assert len(dangling) >= summary.defects["contact_dangling_account"]
