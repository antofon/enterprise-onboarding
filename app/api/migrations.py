"""transform, validate, rehearse.

`GET  /projects/{id}/transformation-plan` says what will happen to every approved column before
anything runs. `POST /projects/{id}/validate` transforms and checks and writes nothing.
`POST /projects/{id}/migrations/dry-run` goes on to send every valid record to the target api in
a staging namespace of its own, and `GET /projects/{id}/migrations/{run_id}` reads the result back
with its issues and refusals.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.db import DbSession
from app.core.http import HttpClient
from app.models.migration import RunKind
from app.schemas.common import ErrorEnvelope
from app.schemas.migration import (
    DryRunRequest,
    FailureRead,
    IssueBreakdownRow,
    IssueRead,
    RunRead,
    RunSummary,
    TransformationPlanRead,
    ValidateRequest,
)
from app.services import migration as migration_service
from app.services.migration import RunOptions
from app.services.projects import get_project
from app.services.transform import build_plan

router = APIRouter(tags=["transformation and migration"])

NOT_FOUND = {404: {"model": ErrorEnvelope}}
RUN_ERRORS = {
    404: {"model": ErrorEnvelope, "description": "no such project"},
    409: {
        "model": ErrorEnvelope,
        "description": "mappings are not fully reviewed, so there is nothing to transform",
    },
    500: {"model": ErrorEnvelope, "description": "the transformation configuration is unusable"},
}


@router.get(
    "/projects/{project_id}/transformation-plan",
    response_model=TransformationPlanRead,
    summary="what the approved mappings mean, column by column, before any row runs",
    responses=RUN_ERRORS,
)
def transformation_plan(project_id: uuid.UUID, session: DbSession) -> TransformationPlanRead:
    project = get_project(session, project_id)
    return TransformationPlanRead.model_validate(build_plan(session, project).as_dict())


@router.post(
    "/projects/{project_id}/validate",
    response_model=RunRead,
    summary="transform and check every record, write nothing",
    responses=RUN_ERRORS,
)
def validate_project(
    project_id: uuid.UUID, body: ValidateRequest, session: DbSession, http: HttpClient
) -> RunRead:
    project = get_project(session, project_id)
    run = migration_service.run_validation(
        session,
        project,
        http_client=http,
        options=RunOptions(
            entities=tuple(body.entities) if body.entities else None,
            limit_per_entity=body.limit_per_entity,
        ),
    )
    return RunRead.model_validate(run)


@router.post(
    "/projects/{project_id}/migrations/dry-run",
    response_model=RunRead,
    summary="rehearse the migration against the target api in a staging namespace",
    responses=RUN_ERRORS,
)
def dry_run(
    project_id: uuid.UUID, body: DryRunRequest, session: DbSession, http: HttpClient
) -> RunRead:
    project = get_project(session, project_id)
    run = migration_service.run_dry_run(
        session,
        project,
        http_client=http,
        options=RunOptions(
            entities=tuple(body.entities) if body.entities else None,
            limit_per_entity=body.limit_per_entity,
            stop_after_failures=body.stop_after_failures,
            purge_namespace_after=body.purge_namespace_after,
        ),
    )
    return RunRead.model_validate(run)


@router.get(
    "/projects/{project_id}/migrations",
    response_model=list[RunSummary],
    summary="every run on this project, newest first",
    responses=NOT_FOUND,
)
def list_runs(
    project_id: uuid.UUID,
    session: DbSession,
    kind: Annotated[RunKind | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunSummary]:
    project = get_project(session, project_id)
    runs = migration_service.list_runs(session, project, kind=kind, limit=limit)
    return [RunSummary.model_validate(r) for r in runs]


@router.get(
    "/projects/{project_id}/migrations/{run_id}",
    response_model=RunRead,
    summary="one run with its counts",
    responses=NOT_FOUND,
)
def get_run(project_id: uuid.UUID, run_id: uuid.UUID, session: DbSession) -> RunRead:
    project = get_project(session, project_id)
    return RunRead.model_validate(migration_service.get_run(session, project, run_id))


@router.get(
    "/projects/{project_id}/migrations/{run_id}/issues",
    response_model=list[IssueRead],
    summary="what validation found, filterable by entity, severity and error type",
    responses=NOT_FOUND,
)
def run_issues(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DbSession,
    entity: Annotated[str | None, Query()] = None,
    severity: Annotated[str | None, Query()] = None,
    error_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[IssueRead]:
    project = get_project(session, project_id)
    run = migration_service.get_run(session, project, run_id)
    rows = migration_service.run_issues(
        session,
        run,
        entity=entity,
        severity=severity,
        error_type=error_type,
        limit=limit,
        offset=offset,
    )
    return [IssueRead.model_validate(r) for r in rows]


@router.get(
    "/projects/{project_id}/migrations/{run_id}/issue-breakdown",
    response_model=list[IssueBreakdownRow],
    summary="issue counts by entity, severity and error type",
    responses=NOT_FOUND,
)
def issue_breakdown(
    project_id: uuid.UUID, run_id: uuid.UUID, session: DbSession
) -> list[IssueBreakdownRow]:
    project = get_project(session, project_id)
    run = migration_service.get_run(session, project, run_id)
    return [
        IssueBreakdownRow.model_validate(row)
        for row in migration_service.issue_breakdown(session, run)
    ]


@router.get(
    "/projects/{project_id}/migrations/{run_id}/failures",
    response_model=list[FailureRead],
    summary="records the target refused, and records never attempted because their parent was",
    responses=NOT_FOUND,
)
def run_failures(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DbSession,
    entity: Annotated[str | None, Query()] = None,
    error_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[FailureRead]:
    project = get_project(session, project_id)
    run = migration_service.get_run(session, project, run_id)
    rows = migration_service.run_failures(
        session, run, entity=entity, error_type=error_type, limit=limit, offset=offset
    )
    return [FailureRead.model_validate(r) for r in rows]
