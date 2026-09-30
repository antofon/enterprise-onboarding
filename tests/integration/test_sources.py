import json
from pathlib import Path

import pytest

from app.core.http import get_http_client

pytestmark = pytest.mark.integration

MANIFEST = json.loads(Path("sample_customer/data/manifest.json").read_text())
BILLING_URL = "http://testserver/mock/billing/v1/subscriptions"


def _project(client) -> str:
    r = client.post(
        "/api/v1/projects",
        json={"customer_name": "Apex Equipment Services", "project_name": "go-live"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _attach(client, pid: str, name: str, kind: str, location: str):
    return client.post(
        f"/api/v1/projects/{pid}/sources",
        json={"name": name, "kind": kind, "location": location},
    )


def test_available_sources_list_the_sample_and_the_billing_feed(client) -> None:
    r = client.get("/api/v1/sources/available")
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()}
    assert by_name["organizations.csv"]["location"] == "sample_customer/data/organizations.csv"
    assert by_name["organizations.csv"]["kind"] == "csv"
    assert "manifest.json" not in by_name
    assert by_name["subscriptions (billing api)"]["kind"] == "api"
    assert by_name["subscriptions (billing api)"]["system"] == "LegacyBill 4.2"


def test_attach_profile_and_compare(client) -> None:
    pid = _project(client)
    assert (
        _attach(
            client, pid, "organizations.csv", "csv", "sample_customer/data/organizations.csv"
        ).status_code
        == 201
    )
    assert (
        _attach(client, pid, "contacts.csv", "csv", "sample_customer/data/contacts.csv").status_code
        == 201
    )
    assert (
        _attach(client, pid, "subscriptions (billing api)", "api", BILLING_URL).status_code == 201
    )

    # the billing loader goes through http; hand it the test client so it reaches this app
    client.app.dependency_overrides[get_http_client] = lambda: client
    try:
        r = client.post(f"/api/v1/projects/{pid}/sources/profile")
    finally:
        client.app.dependency_overrides.clear()
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["stage"] == "profiled"
    rows = {d["name"]: d["row_count"] for d in run["datasets"]}
    assert rows == {
        "organizations.csv": MANIFEST["files"]["organizations.csv"],
        "contacts.csv": MANIFEST["files"]["contacts.csv"],
        "subscriptions (billing api)": MANIFEST["files"]["subscriptions.json"],
    }
    keys = {d["name"]: d["key_column"] for d in run["datasets"]}
    assert (
        keys["organizations.csv"] == "acct_num" and keys["subscriptions (billing api)"] == "sub_id"
    )
    assert run["datasets"][0]["issues"]["error"] > 0

    project = client.get(f"/api/v1/projects/{pid}").json()
    assert project["stage"] == "profiled"
    orgs = next(d for d in project["datasets"] if d["name"] == "organizations.csv")
    assert orgs["row_count"] == MANIFEST["files"]["organizations.csv"]
    assert orgs["quality"]["key_missing"] == MANIFEST["defects"]["org_missing_id"]
    assert orgs["quality"]["issue_counts"]["error"] >= 3

    detail = client.get(f"/api/v1/projects/{pid}/sources/{orgs['id']}").json()
    fields = {f["name"]: f for f in detail["fields"]}
    assert fields["created_on"]["inferred_type"] == "date"
    assert len(fields["created_on"]["stats"]["format_counts"]) == 6
    assert fields["primary_contact_email"]["stats"]["malformed_count"] > 0
    assert fields["acct_num"]["position"] == 0

    contacts = next(d for d in project["datasets"] if d["name"] == "contacts.csv")
    refs = contacts["quality"]["references"]
    assert refs[0]["references"] == "organizations.csv.acct_num"
    assert refs[0]["orphans"] >= MANIFEST["defects"]["contact_dangling_account"]

    cmp = client.get(f"/api/v1/projects/{pid}/schema-comparison")
    assert cmp.status_code == 200, cmp.text
    report = cmp.json()
    assert report["datasets"]["subscriptions (billing api)"] == "subscription"
    assert report["counts"]["TRANSFORMATION_REQUIRED"] > 5
    by_field = {(f["dataset"], f["source_field"]): f for f in report["fields"]}
    assert by_field[("contacts.csv", "contact_mail")]["target"] == "contact.email"
    assert by_field[("organizations.csv", "customer_tier")]["classification"] == "UNMAPPED"
    coverage = {c["entity"]: c for c in report["coverage"]}
    assert "subscription.plan" in coverage["subscription"]["missing"]

    # profiling again is allowed and replaces the previous profile
    client.app.dependency_overrides[get_http_client] = lambda: client
    try:
        again = client.post(f"/api/v1/projects/{pid}/sources/profile")
    finally:
        client.app.dependency_overrides.clear()
    assert again.status_code == 200
    detail_again = client.get(f"/api/v1/projects/{pid}/sources/{orgs['id']}").json()
    assert len(detail_again["fields"]) == len(detail["fields"])


def test_attach_rejects_bad_locations(client) -> None:
    pid = _project(client)
    r = _attach(client, pid, "passwd", "csv", "../../etc/passwd")
    assert r.status_code == 422 and r.json()["error"]["type"] == "invalid_location"
    r = _attach(client, pid, "pyproject", "csv", "pyproject.toml")
    assert r.status_code == 422
    r = _attach(client, pid, "missing", "csv", "sample_customer/data/missing.csv")
    assert r.status_code == 404
    r = _attach(client, pid, "feed", "api", "not a url")
    assert r.status_code == 422
    assert (
        _attach(client, pid, "orgs", "csv", "sample_customer/data/organizations.csv").status_code
        == 201
    )
    r = _attach(client, pid, "orgs", "csv", "sample_customer/data/contacts.csv")
    assert r.status_code == 409 and r.json()["error"]["type"] == "conflict"


def test_profile_and_compare_need_sources_first(client) -> None:
    pid = _project(client)
    r = client.post(f"/api/v1/projects/{pid}/sources/profile")
    assert r.status_code == 409 and r.json()["error"]["type"] == "invalid_state"
    r = client.get(f"/api/v1/projects/{pid}/schema-comparison")
    assert r.status_code == 409


def test_unreachable_feed_is_a_502_not_a_crash(client) -> None:
    pid = _project(client)
    _attach(client, pid, "billing", "api", "http://testserver/mock/billing/v1/nope")
    client.app.dependency_overrides[get_http_client] = lambda: client
    try:
        r = client.post(f"/api/v1/projects/{pid}/sources/profile")
    finally:
        client.app.dependency_overrides.clear()
    assert r.status_code == 502, r.text
    assert r.json()["error"]["type"] == "source_unavailable"
    assert client.get(f"/api/v1/projects/{pid}").json()["stage"] == "created"


def test_detach_source(client) -> None:
    pid = _project(client)
    ds = _attach(client, pid, "orgs", "csv", "sample_customer/data/organizations.csv").json()
    assert client.delete(f"/api/v1/projects/{pid}/sources/{ds['id']}").status_code == 204
    assert client.get(f"/api/v1/projects/{pid}/sources").json() == []
    assert client.delete(f"/api/v1/projects/{pid}/sources/{ds['id']}").status_code == 404
