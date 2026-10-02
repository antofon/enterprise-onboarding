"""get a source into a dataframe of raw values without letting pandas guess anything.

every csv cell arrives as the string that was in the file (zip codes stay strings, blanks stay
blank). json and api records keep their json types so the profiler can report that a column is
an int in 70% of records and a string in the rest, which is a real onboarding finding.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from app.core.config import Settings, get_settings
from app.core.errors import AppError, NotFoundError, SourceUnavailableError
from app.core.http import retry_after_seconds
from app.core.logging import get_logger
from app.models.project import DatasetKind

log = get_logger(__name__)

MAX_PAGES = 10_000
# worth asking again: the feed is busy or briefly broken. a 401 or a 404 will not change.
RETRYABLE_FEED_STATUS = (408, 429, 500, 502, 503, 504)


def resolve_source_path(location: str, roots: list[str] | None = None) -> Path:
    """a file under one of the allowed source roots, or an error. absolute paths and `..` are
    refused before the filesystem is touched."""
    roots = roots if roots is not None else get_settings().source_root_paths
    if not location or location.startswith(("/", "\\")) or ".." in Path(location).parts:
        raise AppError(
            "source location must be a relative path under one of the source roots",
            status_code=422,
            error_type="invalid_location",
            details={"location": location, "roots": roots},
        )
    candidate = Path(location).resolve()
    for root in roots:
        root_path = Path(root).resolve()
        if candidate == root_path or root_path in candidate.parents:
            if not candidate.is_file():
                raise NotFoundError(
                    f"no such source file: {location}", details={"location": location}
                )
            return candidate
    raise AppError(
        f"{location} is outside the allowed source roots",
        status_code=422,
        error_type="invalid_location",
        details={"location": location, "roots": roots},
    )


def load_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a source problem
        raise SourceUnavailableError(
            f"could not read {path.name} as csv", details={"reason": str(exc)[:200]}
        ) from exc


def _records_from_json(payload: Any, where: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        payload = payload["data"]
    if not isinstance(payload, list) or (payload and not isinstance(payload[0], dict)):
        raise SourceUnavailableError(
            f"{where}: expected a list of records or an export envelope with a `data` list"
        )
    return payload


def load_json(path: Path) -> pd.DataFrame:
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SourceUnavailableError(
            f"could not read {path.name} as json", details={"reason": str(exc)[:200]}
        ) from exc
    return _frame_from_records(_records_from_json(payload, path.name))


def load_api(
    url: str,
    *,
    token: str | None,
    page_size: int,
    http_client: httpx.Client | None = None,
    settings: Settings | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    """page through a LegacyBill-style feed: ?page=&page_size=, bearer token, has_more.

    A page that answers 429 or a 5xx, or does not answer, is asked again: after what Retry-After
    says (capped) when the feed says it, after a short linear backoff when it does not. A feed
    still failing after the last attempt is a 502 `source_unavailable` naming the page."""
    settings = settings or get_settings()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = http_client or httpx.Client(timeout=30)
    records: list[dict[str, Any]] = []
    retries = 0
    try:
        for page in range(1, MAX_PAGES + 1):
            response, used = _get_page(
                client, url, page, page_size, headers, settings=settings, sleep=sleep
            )
            retries += used - 1
            body = response.json()
            chunk = _records_from_json(body, url)
            records.extend(chunk)
            has_more = body.get("has_more") if isinstance(body, dict) else None
            if has_more is False or (has_more is None and len(chunk) < page_size) or not chunk:
                break
        log.info(
            "api_source_loaded", url=url, pages=page, record_count=len(records), retries=retries
        )
    finally:
        if http_client is None:
            client.close()
    return _frame_from_records(records)


def _get_page(
    client: httpx.Client,
    url: str,
    page: int,
    page_size: int,
    headers: dict[str, str],
    *,
    settings: Settings,
    sleep: Callable[[float], None],
) -> tuple[httpx.Response, int]:
    """one page, retried while the failure is one that waiting can fix."""
    attempts = max(1, settings.billing_max_attempts)
    for attempt in range(1, attempts + 1):
        problem: str
        wait: float | None = None
        try:
            r = client.get(url, params={"page": page, "page_size": page_size}, headers=headers)
        except httpx.HTTPError as exc:
            problem = f"unreachable: {exc.__class__.__name__}"
            status = None
        else:
            if r.status_code == 200:
                return r, attempt
            status = r.status_code
            problem = f"answered {status}"
            if status not in RETRYABLE_FEED_STATUS:
                raise SourceUnavailableError(
                    f"billing feed {problem}",
                    details={"url": url, "page": page, "body": r.text[:200]},
                )
            wait = retry_after_seconds(r)
        if attempt == attempts:
            raise SourceUnavailableError(
                f"billing feed {problem} on page {page} after {attempts} attempts",
                details={"url": url, "page": page, "attempts": attempts, "status": status},
            )
        delay = (
            min(wait, settings.billing_retry_max_wait_seconds)
            if wait is not None
            else settings.billing_retry_backoff_seconds * attempt
        )
        log.warning(
            "source_retry",
            url=url,
            page=page,
            attempt=attempt,
            status=status,
            problem=problem,
            wait_seconds=delay,
            retry_after=wait,
        )
        sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _frame_from_records(records: list[dict[str, Any]]) -> pd.DataFrame:
    # object dtype on purpose: the profiler wants to see the json types as they came
    columns: list[str] = []
    for rec in records:
        for key in rec:
            if key not in columns:
                columns.append(key)
    data = {c: [rec.get(c) for rec in records] for c in columns}
    return pd.DataFrame(data, columns=columns, dtype=object)


def load_source(
    kind: DatasetKind | str,
    location: str,
    *,
    settings: Settings | None = None,
    http_client: httpx.Client | None = None,
) -> pd.DataFrame:
    settings = settings or get_settings()
    kind = DatasetKind(kind)
    if kind == DatasetKind.api:
        return load_api(
            location,
            token=settings.billing_api_token,
            page_size=settings.billing_page_size,
            http_client=http_client,
            settings=settings,
        )
    path = resolve_source_path(location, settings.source_root_paths)
    if kind == DatasetKind.csv:
        return load_csv(path)
    return load_json(path)
