"""one outbound http client per request. injected so tests can hand the app its own test
client and the billing feed loader still goes through http."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import httpx
from fastapi import Depends


def get_http_client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        yield client


HttpClient = Annotated[httpx.Client, Depends(get_http_client)]
