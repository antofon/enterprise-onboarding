from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app.core.db import DbSession
from app.core.http import HttpClient
from app.schemas.common import ErrorEnvelope
from app.schemas.project import SourceDatasetRead
from app.schemas.sources import (
    AvailableSource,
    ProfileRunRead,
    SourceAttach,
    SourceDatasetDetail,
)
from app.services import sources as source_service
from app.services.comparison import ComparisonReport
from app.services.projects import get_project

router = APIRouter(tags=["sources"])

NOT_FOUND = {404: {"model": ErrorEnvelope}}
ATTACH_ERRORS = {
    404: {"model": ErrorEnvelope, "description": "no such project or file"},
    409: {"model": ErrorEnvelope, "description": "a source with that name is already attached"},
    422: {"model": ErrorEnvelope, "description": "location outside the source roots"},
}
PROFILE_ERRORS = {
    404: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope, "description": "nothing attached yet"},
    502: {"model": ErrorEnvelope, "description": "a source file or feed could not be read"},
}


@router.get(
    "/sources/available",
    response_model=list[AvailableSource],
    summary="files under the source roots and known feeds, ready to attach",
)
def list_available() -> list[AvailableSource]:
    return source_service.available_sources()


@router.get(
    "/projects/{project_id}/sources", response_model=list[SourceDatasetRead], responses=NOT_FOUND
)
def list_sources(project_id: uuid.UUID, session: DbSession) -> list[SourceDatasetRead]:
    project = get_project(session, project_id)
    return [SourceDatasetRead.model_validate(d) for d in project.datasets]


@router.post(
    "/projects/{project_id}/sources",
    response_model=SourceDatasetRead,
    status_code=status.HTTP_201_CREATED,
    summary="attach a source file or feed to the project",
    responses=ATTACH_ERRORS,
)
def attach_source(
    project_id: uuid.UUID, body: SourceAttach, session: DbSession
) -> SourceDatasetRead:
    project = get_project(session, project_id)
    return SourceDatasetRead.model_validate(source_service.attach_source(session, project, body))


@router.post(
    "/projects/{project_id}/sources/profile",
    response_model=ProfileRunRead,
    summary="profile every attached source and check keys across them",
    responses=PROFILE_ERRORS,
)
def profile_sources(project_id: uuid.UUID, session: DbSession, http: HttpClient) -> ProfileRunRead:
    project = get_project(session, project_id)
    return source_service.profile_project(session, project, http_client=http)


@router.get(
    "/projects/{project_id}/sources/{dataset_id}",
    response_model=SourceDatasetDetail,
    summary="one source with its per-column profile",
    responses=NOT_FOUND,
)
def get_source(
    project_id: uuid.UUID, dataset_id: uuid.UUID, session: DbSession
) -> SourceDatasetDetail:
    project = get_project(session, project_id)
    return SourceDatasetDetail.model_validate(
        source_service.get_source(session, project, dataset_id)
    )


@router.delete(
    "/projects/{project_id}/sources/{dataset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND,
)
def detach_source(project_id: uuid.UUID, dataset_id: uuid.UUID, session: DbSession) -> Response:
    project = get_project(session, project_id)
    source_service.delete_source(session, project, dataset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/projects/{project_id}/schema-comparison",
    response_model=ComparisonReport,
    summary="deterministic classification of every source field against the target catalog",
    responses={404: {"model": ErrorEnvelope}, 409: {"model": ErrorEnvelope}},
)
def schema_comparison(project_id: uuid.UUID, session: DbSession) -> ComparisonReport:
    project = get_project(session, project_id)
    return source_service.comparison_report(session, project)
