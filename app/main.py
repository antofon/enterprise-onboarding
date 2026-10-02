from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response

from app import __version__
from app.api.router import api_router
from app.core.config import get_settings
from app.core.db import init_db
from app.core.errors import install_error_handlers, unhandled_error_response
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)

# a caller may bring its own request id (the migration runner does, per record); anything that
# does not look like one is replaced, so a header cannot write arbitrary text into the logs
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,100}")

DESCRIPTION = """
Internal implementation tool for onboarding a new enterprise customer's data into the platform.

Profile the customer's source data, compare it with the target schema, review AI-proposed field
mappings, resolve open questions with the customer, transform and validate deterministically,
dry-run the migration against the target API, reconcile, and report readiness.

The customer and the target platform are fictional. The data is synthetic.
"""


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    init_db()
    log.info(
        "startup",
        version=__version__,
        env=settings.app_env,
        llm_provider=settings.effective_llm_provider,
        llm_model=settings.llm_model,
    )
    yield
    log.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Enterprise Onboarding",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        offered = request.headers.get("x-request-id") or ""
        request_id = offered if _REQUEST_ID.fullmatch(offered) else uuid.uuid4().hex[:12]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=request.method, path=request.url.path
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - turned into the error envelope, logged there
            # anything the routers' handlers did not answer ends here rather than in
            # starlette's plain-text 500, so it keeps the envelope, the header and the log line
            response = unhandled_error_response(exc)
        response.headers["x-request-id"] = request_id
        if request.url.path != "/health" or response.status_code != 200:
            log.info(
                "request",
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        return response

    install_error_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
