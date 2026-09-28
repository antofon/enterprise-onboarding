from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.db import DbSession
from app.schemas.common import ErrorEnvelope
from app.schemas.project import ProjectCreate, ProjectRead, ProjectSummary
from app.services import projects as project_service

router = APIRouter(prefix="/projects", tags=["projects"])

NOT_FOUND = {404: {"model": ErrorEnvelope, "description": "no project with that id"}}


@router.post(
    "",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="open an onboarding project for a customer",
    responses={422: {"model": ErrorEnvelope}},
)
def create_project(body: ProjectCreate, session: DbSession) -> ProjectRead:
    project = project_service.create_project(session, body)
    return ProjectRead.model_validate(project)


@router.get("", response_model=list[ProjectSummary], summary="newest first")
def list_projects(session: DbSession) -> list[ProjectSummary]:
    return [ProjectSummary.model_validate(p) for p in project_service.list_projects(session)]


@router.get("/{project_id}", response_model=ProjectRead, responses=NOT_FOUND)
def get_project(project_id: uuid.UUID, session: DbSession) -> ProjectRead:
    return ProjectRead.model_validate(project_service.get_project(session, project_id))
