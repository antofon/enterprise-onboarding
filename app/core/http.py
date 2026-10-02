"""one outbound http client per request. injected so tests can hand the app its own test
client and the billing feed loader still goes through http."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Annotated

import httpx
from fastapi import Depends


def get_http_client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        yield client


HttpClient = Annotated[httpx.Client, Depends(get_http_client)]


def retry_after_seconds(response: httpx.Response, *, now: datetime | None = None) -> float | None:
    """what a 429 or a 503 asks the caller to wait: Retry-After as seconds or as an http date.
    None when the header is absent or unreadable, so the caller falls back to its own backoff."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())
