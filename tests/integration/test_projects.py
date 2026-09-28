import uuid

import pytest

pytestmark = pytest.mark.integration

PAYLOAD = {
    "customer_name": "Apex Equipment Services",
    "project_name": "Apex go-live migration",
    "source_systems": ["legacy crm export", "billing api", "business rules doc"],
    "notes": "kickoff 2026-09-28, go-live target in three weeks",
}


def test_create_then_fetch_project(client) -> None:
    created = client.post("/api/v1/projects", json=PAYLOAD)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["stage"] == "created"
    assert body["target_environment"] == "staging"
    assert body["datasets"] == []

    fetched = client.get(f"/api/v1/projects/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["customer_name"] == "Apex Equipment Services"

    listed = client.get("/api/v1/projects")
    assert [p["id"] for p in listed.json()] == [body["id"]]


def test_unknown_project_uses_error_envelope(client) -> None:
    r = client.get(f"/api/v1/projects/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "not_found"


def test_blank_names_are_rejected(client) -> None:
    r = client.post("/api/v1/projects", json={"customer_name": "   ", "project_name": "x"})
    assert r.status_code == 422
    assert r.json()["error"]["type"] == "validation_error"
