from __future__ import annotations

from fastapi import APIRouter

from app.api import billing, health, projects

api_router = APIRouter()
api_router.include_router(health.router)

v1 = APIRouter(prefix="/api/v1")
v1.include_router(projects.router)
api_router.include_router(v1)

# simulated external systems live under their own prefixes, outside /api/v1
api_router.include_router(billing.router)
