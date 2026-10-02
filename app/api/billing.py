"""the customer's legacy billing system, simulated.

LegacyBill 4.2 exposes subscriptions over a paginated, token-protected read-only api. this router
plays that system so the profiler has a real http source to page through (auth header, page size,
has_more, upstream errors) instead of just reading a file. the records come from the synthetic
customer's subscriptions.json, so the api and the file agree.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.errors import RateLimitedError, SourceUnavailableError, UnauthorizedError
from app.schemas.common import ErrorEnvelope

router = APIRouter(prefix="/mock/billing/v1", tags=["mock billing source"])


class BillingPage(BaseModel):
    system: str
    exported_at: str
    page: int
    page_size: int
    total: int
    has_more: bool
    data: list[dict[str, Any]]


_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _load_export(path: str) -> dict[str, Any]:
    """the export file, re-read only when it changes on disk."""
    file = Path(path)
    try:
        mtime = file.stat().st_mtime
    except FileNotFoundError as exc:
        raise SourceUnavailableError("billing export not found", details={"path": path}) from exc
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    export = json.loads(file.read_text())
    if not isinstance(export, dict) or not isinstance(export.get("data"), list):
        raise SourceUnavailableError(
            "billing export is not a LegacyBill envelope", details={"path": path}
        )
    _cache[path] = (mtime, export)
    return export


class _Window:
    """LegacyBill's rate limit: at most `limit` requests in any `window` seconds, per process.
    Past it, a 429 whose Retry-After says when the oldest request in the window expires."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: deque[float] = deque()

    def admit(self, limit: int, window: float) -> int | None:
        """None when the request may go ahead, otherwise the seconds to wait."""
        if limit <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            while self._seen and now - self._seen[0] >= window:
                self._seen.popleft()
            if len(self._seen) < limit:
                self._seen.append(now)
                return None
            return max(1, math.ceil(window - (now - self._seen[0])))

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()


rate_window = _Window()


def _require_token(authorization: str | None) -> None:
    expected = f"Bearer {get_settings().billing_api_token}"
    if authorization != expected:
        raise UnauthorizedError(
            "LegacyBill: missing or invalid bearer token",
            details={"hint": "Authorization: Bearer <BILLING_API_TOKEN>"},
        )


@router.get(
    "/subscriptions",
    response_model=BillingPage,
    summary="paginated subscription records, bearer token required",
    responses={
        401: {"model": ErrorEnvelope},
        429: {"model": ErrorEnvelope, "description": "over the rate limit; see Retry-After"},
        502: {"model": ErrorEnvelope},
    },
)
def list_subscriptions(
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
    authorization: str | None = Header(default=None),
) -> BillingPage:
    _require_token(authorization)
    settings = get_settings()
    wait = rate_window.admit(settings.billing_rate_limit, settings.billing_rate_window_seconds)
    if wait is not None:
        raise RateLimitedError(
            f"LegacyBill: more than {settings.billing_rate_limit} requests in "
            f"{settings.billing_rate_window_seconds:g}s",
            retry_after=wait,
        )
    export = _load_export(settings.billing_source_file)
    records = export["data"]
    start = (page - 1) * page_size
    chunk = records[start : start + page_size]
    return BillingPage(
        system=export.get("system", "LegacyBill"),
        exported_at=export.get("exported_at", ""),
        page=page,
        page_size=page_size,
        total=len(records),
        has_more=start + page_size < len(records),
        data=chunk,
    )
