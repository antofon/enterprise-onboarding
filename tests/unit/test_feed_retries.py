"""the billing feed loader when the customer's api is busy, flaky or down.

A rate limit or a blip is waited out; a feed that stays down, or answers in a way waiting cannot
fix, is a 502 that names the page. The feed is stubbed so every answer can be forced, and sleeps
are recorded instead of slept.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import SourceUnavailableError
from app.services.profiling.loaders import load_api

URL = "http://legacybill.test/subscriptions"


def page(n: int, more: bool) -> httpx.Response:
    return httpx.Response(
        200, json={"data": [{"sub_id": f"S-{n}-{i}"} for i in range(2)], "has_more": more}
    )


def feed(answers: list[httpx.Response]):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return answers.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def load(client: httpx.Client, sleeps: list[float], **overrides):
    settings = Settings(billing_retry_backoff_seconds=0.5, **overrides)
    return load_api(
        URL, token="t", page_size=2, http_client=client, settings=settings, sleep=sleeps.append
    )


def test_a_rate_limit_is_waited_out_as_long_as_the_feed_asks() -> None:
    client, calls = feed(
        [
            page(1, True),
            httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {}}),
            page(2, False),
        ]
    )
    sleeps: list[float] = []
    frame = load(client, sleeps)
    assert len(frame) == 4
    assert sleeps == [2.0]
    assert [c.url.params["page"] for c in calls] == ["1", "2", "2"]


def test_a_blip_without_retry_after_gets_a_linear_backoff() -> None:
    client, _ = feed(
        [httpx.Response(503), httpx.Response(502), page(1, False)],
    )
    sleeps: list[float] = []
    assert len(load(client, sleeps)) == 2
    assert sleeps == [0.5, 1.0]


def test_a_feed_that_stays_down_is_a_502_naming_the_page_and_the_attempts() -> None:
    client, calls = feed([httpx.Response(500)] * 4)
    sleeps: list[float] = []
    with pytest.raises(SourceUnavailableError) as raised:
        load(client, sleeps, billing_max_attempts=4)
    assert raised.value.status_code == 502
    assert raised.value.details["page"] == 1
    assert raised.value.details["attempts"] == 4
    assert "after 4 attempts" in raised.value.message
    assert len(calls) == 4 and len(sleeps) == 3


def test_a_wrong_token_is_not_retried() -> None:
    client, calls = feed([httpx.Response(401, json={"error": {"type": "unauthorized"}})])
    with pytest.raises(SourceUnavailableError) as raised:
        load(client, [])
    assert "401" in raised.value.message
    assert len(calls) == 1


def test_a_feed_that_does_not_answer_is_retried_then_reported() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    with pytest.raises(SourceUnavailableError) as raised:
        load(client, sleeps, billing_max_attempts=3)
    assert "unreachable: ConnectError" in raised.value.message
    assert attempts == 3 and sleeps == [0.5, 1.0]


def test_the_retry_after_wait_is_capped() -> None:
    client, _ = feed(
        [httpx.Response(429, headers={"Retry-After": "3600"}), page(1, False)],
    )
    sleeps: list[float] = []
    load(client, sleeps, billing_retry_max_wait_seconds=10.0)
    assert sleeps == [10.0]
