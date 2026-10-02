"""the readiness report.

`POST /projects/{id}/reports/readiness` generates and stores one. `GET` on the same path reads
the latest back, as json or as the markdown the customer is sent; with nothing stored yet it
answers with a preview computed now, which is marked as such and stored nowhere.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse, Response

from app.ai.provider import LlmProviderDep
from app.core.db import DbSession
from app.models.report import ReadinessReport, ReadinessStatus
from app.schemas.common import ErrorEnvelope, Responses
from app.schemas.report import ReportListRow, ReportRead, ReportRequest
from app.services import readiness as readiness_service
from app.services.projects import get_project

router = APIRouter(tags=["readiness report"])

NOT_FOUND: Responses = {404: {"model": ErrorEnvelope}}
Format = Annotated[Literal["json", "markdown"], Query(alias="format")]


def _read(report: ReadinessReport) -> ReportRead:
    return ReportRead(
        id=report.id,
        project_id=report.project_id,
        stored=True,
        status=report.status,
        summary_origin=report.summary_origin,
        run_id=report.run_id,
        reconciliation_id=report.reconciliation_id,
        generated_by=report.generated_by,
        created_at=report.created_at,
        content=report.content,
    )


def _filename(customer: str, report_id: uuid.UUID | None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", customer.lower()).strip("-") or "customer"
    return f"readiness-{slug}-{str(report_id)[:8] if report_id else 'preview'}.md"


def _markdown(text: str, customer: str, report_id: uuid.UUID | None) -> Response:
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_filename(customer, report_id)}"'},
    )


@router.post(
    "/projects/{project_id}/reports/readiness",
    response_model=ReportRead,
    status_code=201,
    summary="generate and store a readiness report from the latest dry run",
    responses=NOT_FOUND,
)
def generate(
    project_id: uuid.UUID, body: ReportRequest, session: DbSession, provider: LlmProviderDep
) -> ReportRead:
    project = get_project(session, project_id)
    report = readiness_service.generate_report(
        session,
        project,
        provider=provider,
        use_model=body.use_model,
        generated_by=body.generated_by,
    )
    return _read(report)


@router.get(
    "/projects/{project_id}/reports/readiness",
    response_model=ReportRead,
    summary="the latest readiness report as json or markdown; a preview when none is stored",
    responses={
        **NOT_FOUND,
        200: {
            "content": {"text/markdown": {}},
            "description": "json, or markdown with ?format=markdown",
        },
    },
)
def latest(
    project_id: uuid.UUID, session: DbSession, fmt: Format = "json"
) -> ReportRead | Response:
    project = get_project(session, project_id)
    report = readiness_service.latest_report(session, project)
    if report is not None:
        if fmt == "markdown":
            return _markdown(report.markdown, project.customer_name, report.id)
        return _read(report)
    content = readiness_service.preview(session, project)
    if fmt == "markdown":
        return _markdown(readiness_service.render_markdown(content), project.customer_name, None)
    return ReportRead(
        id=None,
        project_id=project.id,
        stored=False,
        status=ReadinessStatus(content["status"]),
        summary_origin="template",
        content=content,
    )


@router.get(
    "/projects/{project_id}/reports",
    response_model=list[ReportListRow],
    summary="every stored readiness report on this project, newest first",
    responses=NOT_FOUND,
)
def list_reports(
    project_id: uuid.UUID,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ReportListRow]:
    project = get_project(session, project_id)
    return [
        ReportListRow(
            id=r.id,
            status=r.status,
            summary_origin=r.summary_origin,
            run_id=r.run_id,
            blockers=len(r.content.get("blockers", [])),
            conditions=len(r.content.get("conditions", [])),
            created_at=r.created_at,
        )
        for r in readiness_service.list_reports(session, project, limit)
    ]


@router.get(
    "/projects/{project_id}/reports/{report_id}",
    response_model=ReportRead,
    summary="one stored readiness report, as json or markdown",
    responses=NOT_FOUND,
)
def get_report(
    project_id: uuid.UUID, report_id: uuid.UUID, session: DbSession, fmt: Format = "json"
) -> ReportRead | Response:
    project = get_project(session, project_id)
    report = readiness_service.get_report(session, project, report_id)
    if fmt == "markdown":
        return _markdown(report.markdown, project.customer_name, report.id)
    return _read(report)
