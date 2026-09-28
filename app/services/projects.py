from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.project import OnboardingProject
from app.schemas.project import ProjectCreate

log = get_logger(__name__)


def create_project(session: Session, data: ProjectCreate) -> OnboardingProject:
    project = OnboardingProject(**data.model_dump())
    session.add(project)
    session.commit()
    session.refresh(project)
    log.info("project_created", project_id=str(project.id), customer=project.customer_name)
    return project


def get_project(session: Session, project_id: uuid.UUID) -> OnboardingProject:
    project = session.execute(
        select(OnboardingProject)
        .where(OnboardingProject.id == project_id)
        .options(selectinload(OnboardingProject.datasets))
    ).scalar_one_or_none()
    if project is None:
        raise NotFoundError(
            f"project {project_id} not found", details={"project_id": str(project_id)}
        )
    return project


def list_projects(session: Session) -> list[OnboardingProject]:
    return list(
        session.execute(
            select(OnboardingProject).order_by(OnboardingProject.created_at.desc())
        ).scalars()
    )
