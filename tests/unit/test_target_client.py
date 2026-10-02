"""the migration's http client: when it retries, when it does not, and what it records.

The target is stubbed here so every answer can be forced. The point of the retries is that a
write carries its own identifier, so repeating one is a no-op rather than a double-write.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.core.config import Settings
from app.services.migration import TargetClient

ORG = {
    "organization_id": "10103",
    "name": "Rivera",
    "lifecycle_status": "active",
    "billing_country": "US",
}


def client_for(handler, **overrides) -> TargetClient:
    settings = Settings(
        target_api_base_url="http://target.test/target/v1",
        target_max_attempts=3,
        target_retry_backoff_seconds=0.0,
        **overrides,
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return TargetClient(http, settings=settings, namespace=uuid.uuid4(), request_prefix="test")


def written(status: int, body: dict | None = None, text: str | None = None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body or {})

    return handler, calls


def test_a_created_record_is_accepted_in_one_call() -> None:
    handler, calls = written(201, {"created": True, "unchanged": False, "id": "10103"})
    result = client_for(handler).write("organization", ORG, "10103")
    assert (result.outcome, result.created, result.attempts) == ("accepted", True, 1)
    assert len(calls) == 1
    assert calls[0].headers["authorization"].startswith("Bearer ")
    assert calls[0].headers["x-meridian-namespace"]
    assert calls[0].headers["x-request-id"] == "test-10103"


def test_a_record_already_there_is_accepted_without_being_created() -> None:
    handler, _ = written(200, {"created": False, "unchanged": True, "id": "10103"})
    result = client_for(handler).write("organization", ORG, "10103")
    assert result.outcome == "accepted"
    assert result.created is False


def test_a_rejection_is_the_targets_answer_and_is_not_retried() -> None:
    for status in (409, 422):
        handler, calls = written(
            status, {"error": {"type": "duplicate_identifier", "message": "already there"}}
        )
        result = client_for(handler).write("organization", ORG, "10103")
        assert result.outcome == "rejected"
        assert result.error_type == "duplicate_identifier"
        assert result.message == "already there"
        assert len(calls) == 1, status


def test_a_server_error_is_retried_up_to_the_limit() -> None:
    handler, calls = written(500, {"error": {"type": "target_unavailable", "message": "down"}})
    result = client_for(handler).write("organization", ORG, "10103")
    assert result.outcome == "failed"
    assert result.error_type == "target_unavailable"
    assert result.attempts == 3
    assert len(calls) == 3


def test_a_retry_that_succeeds_is_an_accepted_record() -> None:
    answers = [
        httpx.Response(503, json={"error": {"type": "target_unavailable", "message": "later"}}),
        httpx.Response(201, json={"created": True, "unchanged": False, "id": "10103"}),
    ]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return answers[len(calls) - 1]

    result = client_for(handler).write("organization", ORG, "10103")
    assert result.outcome == "accepted"
    assert result.attempts == 2


def test_a_timeout_is_retried_and_the_second_answer_is_unchanged() -> None:
    """the write landed before the timeout. the retry gets `unchanged`, which is the whole reason
    records carry their own identifier."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("too slow", request=request)
        return httpx.Response(200, json={"created": False, "unchanged": True, "id": "10103"})

    result = client_for(handler).write("organization", ORG, "10103")
    assert result.outcome == "accepted"
    assert result.created is False
    assert result.attempts == 2


def test_a_timeout_that_never_clears_is_reported_as_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("nothing there", request=request)

    result = client_for(handler).write("organization", ORG, "10103")
    assert result.outcome == "failed"
    assert result.error_type == "timeout"
    assert result.attempts == 3


def test_a_transport_failure_is_reported_not_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    result = client_for(handler).write("organization", ORG, "10103")
    assert result.outcome == "failed"
    assert result.error_type == "transport_error"
    assert "ConnectError" in str(result.message)


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": "probably"},
        {"status": "written"},
    ],
)
def test_a_200_that_is_not_a_write_result_is_a_malformed_response(answer) -> None:
    handler, calls = written(200, answer)
    result = client_for(handler).write("organization", ORG, "10103")
    assert result.outcome == "failed"
    assert result.error_type == "malformed_response"
    assert result.attempts == 3
    assert len(calls) == 3


def test_a_200_that_is_not_even_json_is_a_malformed_response() -> None:
    handler, _ = written(200, text="<html>maintenance</html>")
    result = client_for(handler).write("organization", ORG, "10103")
    assert result.error_type == "malformed_response"
    assert "<html>" in str(result.body)


def test_an_error_body_that_is_not_json_still_reports_the_status() -> None:
    handler, _ = written(418, text="teapot")
    result = client_for(handler).write("organization", ORG, "10103")
    assert result.outcome == "rejected"
    assert result.error_type == "http_418"
    assert result.message == "teapot"


# --- waiting when the target says how long ------------------------------------------------------


def _sequence(*responses: httpx.Response):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[min(len(calls), len(responses)) - 1]

    return handler, calls


def _client_with_sleeps(handler, **overrides) -> tuple[TargetClient, list[float]]:
    sleeps: list[float] = []
    settings = Settings(
        target_api_base_url="http://target.test/target/v1",
        target_max_attempts=3,
        target_retry_backoff_seconds=0.25,
        **overrides,
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = TargetClient(
        http,
        settings=settings,
        namespace=uuid.uuid4(),
        request_prefix="test",
        sleep=sleeps.append,
    )
    return client, sleeps


def test_a_429_with_retry_after_is_waited_out_then_accepted() -> None:
    handler, calls = _sequence(
        httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"type": "rate_limited"}}),
        httpx.Response(201, json={"created": True, "unchanged": False, "id": "10103"}),
    )
    client, sleeps = _client_with_sleeps(handler)
    result = client.write("organization", ORG, "10103")
    assert (result.outcome, result.attempts) == ("accepted", 2)
    assert sleeps == [3.0]
    assert len(calls) == 2


def test_a_retry_after_longer_than_the_cap_is_capped() -> None:
    handler, _ = _sequence(
        httpx.Response(503, headers={"Retry-After": "120"}, json={"error": {"type": "busy"}}),
        httpx.Response(201, json={"created": True, "unchanged": False, "id": "10103"}),
    )
    client, sleeps = _client_with_sleeps(handler, target_retry_max_wait_seconds=10.0)
    assert client.write("organization", ORG, "10103").outcome == "accepted"
    assert sleeps == [10.0]


def test_without_retry_after_the_backoff_is_linear() -> None:
    handler, _ = _sequence(httpx.Response(500, json={"error": {"type": "target_unavailable"}}))
    client, sleeps = _client_with_sleeps(handler)
    result = client.write("organization", ORG, "10103")
    assert (result.outcome, result.attempts, result.http_status) == ("failed", 3, 500)
    # no sleep after the last attempt
    assert sleeps == [0.25, 0.5]


def test_a_rate_limit_that_never_lifts_is_a_failure_with_its_type() -> None:
    handler, calls = _sequence(
        httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"type": "rate_limited"}})
    )
    client, sleeps = _client_with_sleeps(handler)
    result = client.write("organization", ORG, "10103")
    assert (result.outcome, result.error_type, result.retry_after) == ("failed", "rate_limited", 1)
    assert len(calls) == 3 and sleeps == [1.0, 1.0]
