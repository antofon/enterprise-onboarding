"""the mock target platform: what Meridian accepts, refuses, and treats as a no-op.

These are the rules the dry run is rehearsing against, so they are tested on their own,
without any onboarding project involved.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import get_settings
from app.models.target import LIVE_NAMESPACE

pytestmark = pytest.mark.integration

ORG = {
    "organization_id": "10103",
    "name": "Rivera, Garcia and Kirk",
    "lifecycle_status": "active",
    "industry": "energy",
    "billing_country": "CA",
    "billing_region": "AB",
}
CONTACT = {
    "contact_id": "CT-000102",
    "organization_id": "10103",
    "email": "jillian.collins@phillips.com",
    "first_name": "Jillian",
    "last_name": "Collins",
    "role": "primary",
    "is_primary": True,
}
SUB = {
    "subscription_id": "SUB-00415",
    "organization_id": "10103",
    "plan": "enterprise",
    "billing_cycle": "annual",
    "status": "active",
    "seats": 60,
    "start_date": "2023-02-10",
    "renewal_date": "2024-02-10",
    "mrr_usd": "5100.00",
}


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().target_api_token}"}


@pytest.fixture
def ns(auth: dict[str, str]) -> dict[str, str]:
    """a staging namespace of its own, so tests cannot see each other's records."""
    return {**auth, "X-Meridian-Namespace": str(uuid.uuid4())}


def test_a_write_needs_the_bearer_token(client) -> None:
    unauthorized = client.post("/target/v1/organizations", json=ORG)
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["type"] == "unauthorized"
    assert client.get("/target/v1/counts").status_code == 401
    assert (
        client.post(
            "/target/v1/organizations", json=ORG, headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )


def test_the_same_record_twice_is_a_no_op_and_a_changed_one_is_a_conflict(client, ns) -> None:
    created = client.post("/target/v1/organizations", json=ORG, headers=ns)
    assert created.status_code == 201
    assert created.json()["created"] is True

    repeat = client.post("/target/v1/organizations", json=ORG, headers=ns)
    assert repeat.status_code == 200
    assert repeat.json() == {**created.json(), "created": False, "unchanged": True}

    changed = client.post(
        "/target/v1/organizations", json={**ORG, "name": "Rivera Holdings"}, headers=ns
    )
    assert changed.status_code == 409
    error = changed.json()["error"]
    assert error["type"] == "duplicate_identifier"
    assert error["details"]["changed_fields"] == ["name"]


def test_a_child_without_its_organization_is_refused(client, ns) -> None:
    orphan = client.post(
        "/target/v1/contacts", json={**CONTACT, "organization_id": "99999"}, headers=ns
    )
    assert orphan.status_code == 422
    assert orphan.json()["error"]["type"] == "missing_relationship"
    assert orphan.json()["error"]["details"]["organization_id"] == "99999"

    client.post("/target/v1/organizations", json=ORG, headers=ns)
    assert client.post("/target/v1/contacts", json=CONTACT, headers=ns).status_code == 201


def test_one_primary_contact_and_one_email_per_organization(client, ns) -> None:
    client.post("/target/v1/organizations", json=ORG, headers=ns)
    client.post("/target/v1/contacts", json=CONTACT, headers=ns)

    second_primary = client.post(
        "/target/v1/contacts",
        json={**CONTACT, "contact_id": "CT-2", "email": "other@phillips.com"},
        headers=ns,
    )
    assert second_primary.status_code == 409
    assert second_primary.json()["error"]["type"] == "duplicate_primary_contact"

    same_email = client.post(
        "/target/v1/contacts",
        json={**CONTACT, "contact_id": "CT-3", "is_primary": False},
        headers=ns,
    )
    assert same_email.status_code == 409
    assert same_email.json()["error"]["type"] == "duplicate_contact_email"


def test_an_inactive_organization_cannot_hold_an_active_subscription(client, ns) -> None:
    client.post(
        "/target/v1/organizations",
        json={**ORG, "organization_id": "20000", "lifecycle_status": "churned"},
        headers=ns,
    )
    refused = client.post(
        "/target/v1/subscriptions", json={**SUB, "organization_id": "20000"}, headers=ns
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["type"] == "business_rule_violation"

    client.post("/target/v1/organizations", json=ORG, headers=ns)
    assert client.post("/target/v1/subscriptions", json=SUB, headers=ns).status_code == 201


def test_field_rules_come_from_the_pydantic_contract(client, ns) -> None:
    for bad in (
        {**ORG, "billing_country": "USA"},
        {**ORG, "lifecycle_status": "Active"},
        {**ORG, "website": "www.rivera.com"},
        {**ORG, "organization_id": ""},
        {**ORG, "unexpected_field": "x"},
    ):
        answer = client.post("/target/v1/organizations", json=bad, headers=ns)
        assert answer.status_code == 422, bad
        assert answer.json()["error"]["type"] == "validation_error"

    client.post("/target/v1/organizations", json=ORG, headers=ns)
    backwards = client.post(
        "/target/v1/subscriptions", json={**SUB, "renewal_date": "2022-01-01"}, headers=ns
    )
    assert backwards.status_code == 422


def test_namespaces_are_isolated_and_can_be_thrown_away(client, auth, ns) -> None:
    other = {**auth, "X-Meridian-Namespace": str(uuid.uuid4())}
    client.post("/target/v1/organizations", json=ORG, headers=ns)
    client.post("/target/v1/contacts", json=CONTACT, headers=ns)

    # the same id in another namespace is a fresh create, and the live data stays empty
    assert client.post("/target/v1/organizations", json=ORG, headers=other).status_code == 201
    assert client.get("/target/v1/counts", headers=auth).json()["counts"]["organization"] == 0
    # a child cannot reach a parent in a different namespace
    assert client.post("/target/v1/contacts", json=CONTACT, headers=auth).status_code == 422

    assert client.get("/target/v1/counts", headers=ns).json()["counts"] == {
        "organization": 1,
        "contact": 1,
        "subscription": 0,
        "activity": 0,
    }
    purged = client.delete(f"/target/v1/namespaces/{ns['X-Meridian-Namespace']}", headers=auth)
    assert purged.status_code == 200
    assert purged.json()["removed"] == {
        "activity": 0,
        "subscription": 0,
        "contact": 1,
        "organization": 1,
    }
    assert client.get("/target/v1/counts", headers=ns).json()["counts"]["organization"] == 0
    # the other namespace is untouched
    assert client.get("/target/v1/counts", headers=other).json()["counts"]["organization"] == 1


def test_the_live_namespace_cannot_be_purged(client, auth) -> None:
    refused = client.delete(f"/target/v1/namespaces/{LIVE_NAMESPACE}", headers=auth)
    assert refused.status_code == 422
    assert refused.json()["error"]["type"] == "invalid_namespace"


def test_records_can_be_read_back(client, ns) -> None:
    client.post("/target/v1/organizations", json=ORG, headers=ns)
    client.post("/target/v1/contacts", json=CONTACT, headers=ns)

    listed = client.get("/target/v1/organizations", headers=ns).json()
    assert [o["organization_id"] for o in listed] == ["10103"]
    assert listed[0]["billing_region"] == "AB"

    one = client.get("/target/v1/contacts/CT-000102", headers=ns).json()
    assert one["email"] == "jillian.collins@phillips.com"
    assert one["is_primary"] is True

    by_org = client.get("/target/v1/contacts?organization_id=99999", headers=ns).json()
    assert by_org == []
    assert client.get("/target/v1/contacts/nope", headers=ns).status_code == 404
    assert client.get("/target/v1/widgets", headers=ns).status_code == 404


def test_faults_can_be_forced_for_a_rehearsal(client, ns) -> None:
    failed = client.post(
        "/target/v1/organizations", json=ORG, headers={**ns, "X-Meridian-Fault": "server_error"}
    )
    assert failed.status_code == 500
    assert failed.json()["error"]["type"] == "target_unavailable"
    # the failed write left nothing behind
    assert client.get("/target/v1/counts", headers=ns).json()["counts"]["organization"] == 0

    malformed = client.post(
        "/target/v1/organizations", json=ORG, headers={**ns, "X-Meridian-Fault": "malformed"}
    )
    assert malformed.status_code == 200
    assert "created" not in malformed.json()
    # a malformed answer still describes a write that happened, which is why retries are idempotent
    assert client.get("/target/v1/counts", headers=ns).json()["counts"]["organization"] == 1
