from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ErrorBody(BaseModel):
    type: str
    message: str
    details: dict[str, Any] = {}
    # the id on every log line of the request, and in the x-request-id header
    request_id: str | None = None


class ErrorEnvelope(BaseModel):
    """what every non-2xx response looks like."""

    error: ErrorBody
