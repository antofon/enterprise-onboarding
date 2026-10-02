"""reading Retry-After the two ways rfc 9110 allows: seconds, or an http date."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.core.http import retry_after_seconds

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)


def answer(value: str | None) -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": value} if value is not None else {})


def test_seconds() -> None:
    assert retry_after_seconds(answer("7"), now=NOW) == 7.0


def test_an_http_date_is_the_time_left_until_then() -> None:
    assert retry_after_seconds(answer("Fri, 02 Oct 2026 12:00:30 GMT"), now=NOW) == 30.0


def test_a_date_already_past_means_now() -> None:
    assert retry_after_seconds(answer("Fri, 02 Oct 2026 11:59:00 GMT"), now=NOW) == 0.0


def test_absent_or_unreadable_leaves_the_caller_its_own_backoff() -> None:
    assert retry_after_seconds(answer(None), now=NOW) is None
    assert retry_after_seconds(answer("soon"), now=NOW) is None
    assert retry_after_seconds(answer(""), now=NOW) is None
