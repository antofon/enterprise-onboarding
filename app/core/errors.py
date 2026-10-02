"""one error shape for the whole api:

{"error": {"type": "not_found", "message": "...", "details": {...}, "request_id": "..."}}

the request id is the one on every log line of the request and in the x-request-id header, so
whoever sees the error can hand over the one string that finds the logs.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import error_type_of, get_logger

log = get_logger(__name__)


class AppError(Exception):
    status_code = 400
    error_type = "app_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if error_type is not None:
            self.error_type = error_type
        self.details = details or {}

    def to_response(self) -> JSONResponse:
        return error_response(self.status_code, self.error_type, self.message, self.details)


def current_request_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("request_id")
    return None if value is None else str(value)


def error_response(
    status_code: int,
    error_type: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "type": error_type,
                "message": message,
                "details": details or {},
                "request_id": current_request_id(),
            }
        },
        headers=headers,
    )


def unhandled_error_response(exc: Exception) -> JSONResponse:
    """the answer for an exception nothing else handled. the database being down is a 503 the
    caller can wait out; anything else is a 500 that names no internals, only the request id,
    and leaves the traceback in the log where the id finds it."""
    if isinstance(exc, OperationalError) or (
        isinstance(exc, DBAPIError) and exc.connection_invalidated
    ):
        log.error("database_unavailable", error_type=error_type_of(exc), error=str(exc)[:300])
        return error_response(
            503,
            "database_unavailable",
            "the database is not reachable; nothing was changed by this request",
        )
    log.error(
        "unhandled_error",
        error_type=error_type_of(exc),
        error=str(exc)[:300],
        exc_info=not getattr(exc, "_stage_logged", False),
    )
    return error_response(
        500,
        "internal_error",
        "the server hit an unexpected error; quote the request id when reporting it",
    )


class NotFoundError(AppError):
    status_code = 404
    error_type = "not_found"


class ConflictError(AppError):
    status_code = 409
    error_type = "conflict"


class UnauthorizedError(AppError):
    status_code = 401
    error_type = "unauthorized"


class SourceUnavailableError(AppError):
    """a source file or feed could not be read."""

    status_code = 502
    error_type = "source_unavailable"


class InvalidStateError(AppError):
    """the project is not in a stage where this action makes sense."""

    status_code = 409
    error_type = "invalid_state"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        log.warning("app_error", error_type=exc.error_type, message=exc.message, **exc.details)
        return exc.to_response()

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        """fastapi's own 404/405/etc, rewrapped so every error has the same shape."""
        names = {404: "not_found", 405: "method_not_allowed"}
        error_type = names.get(exc.status_code, "http_error")
        return error_response(
            exc.status_code, error_type, str(exc.detail), headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # pydantic's error dicts carry the raw exception under ctx; keep the useful parts only
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return error_response(
            422,
            "validation_error",
            "request did not match the expected shape",
            {"errors": errors},
        )
