from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import __version__
from app.core.config import get_settings
from app.core.db import check_db

router = APIRouter(tags=["ops"])


@router.get("/health", summary="liveness + database reachability")
def health() -> JSONResponse:
    settings = get_settings()
    db_ok = check_db()
    body = {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "version": __version__,
        "llm_provider": settings.effective_llm_provider,
        "llm_model": settings.llm_model,
    }
    return JSONResponse(status_code=200 if db_ok else 503, content=body)
