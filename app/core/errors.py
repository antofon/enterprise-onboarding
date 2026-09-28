"""one error shape for the whole api:

{"error": {"type": "not_found", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

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
        return JSONResponse(
            status_code=self.status_code,
            content={
                "error": {
                    "type": self.error_type,
                    "message": self.message,
                    "details": self.details,
                }
            },
        )


class NotFoundError(AppError):
    status_code = 404
    error_type = "not_found"


class ConflictError(AppError):
    status_code = 409
    error_type = "conflict"


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
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"type": error_type, "message": str(exc.detail), "details": {}}},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "type": "validation_error",
                    "message": "request did not match the expected shape",
                    "details": {"errors": exc.errors()},
                }
            },
        )
