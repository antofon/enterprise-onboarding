"""the customer's legacy billing system, simulated.

LegacyBill 4.2 exposes subscriptions over a paginated, token-protected read-only api. this router
plays that system so the profiler has a real http source to page through (auth header, page size,
has_more, upstream errors) instead of just reading a file. the records come from the synthetic
customer's subscriptions.json, so the api and the file agree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.errors import SourceUnavailableError, UnauthorizedError
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
    responses={401: {"model": ErrorEnvelope}, 502: {"model": ErrorEnvelope}},
)
def list_subscriptions(
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
    authorization: str | None = Header(default=None),
) -> BillingPage:
    _require_token(authorization)
    export = _load_export(get_settings().billing_source_file)
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
