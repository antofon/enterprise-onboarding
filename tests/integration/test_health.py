import pytest

pytestmark = pytest.mark.integration


def test_health_reports_database_ok(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["llm_provider"] == "none"
    assert r.headers["x-request-id"]


def test_unknown_route_uses_error_envelope_shape(client) -> None:
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"error": {"type": "not_found", "message": "Not Found", "details": {}}}

    r = client.post("/health")
    assert r.status_code == 405
    assert r.json()["error"]["type"] == "method_not_allowed"
