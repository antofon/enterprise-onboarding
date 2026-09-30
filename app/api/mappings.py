from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.ai.provider import LlmProviderDep
from app.core.db import DbSession
from app.models.mapping import MappingStatus
from app.schemas.common import ErrorEnvelope
from app.schemas.mapping import (
    BulkApproveRequest,
    BulkApproveResult,
    FieldMappingRead,
    LlmCallRead,
    MappingDecision,
    MappingSummary,
    SuggestRequest,
    SuggestRunRead,
)
from app.services import mapping as mapping_service
from app.services.projects import get_project

router = APIRouter(tags=["mappings"])

NOT_FOUND = {404: {"model": ErrorEnvelope}}
SUGGEST_ERRORS = {
    404: {"model": ErrorEnvelope, "description": "no such project, dataset or rules document"},
    409: {"model": ErrorEnvelope, "description": "sources are not profiled yet"},
    422: {"model": ErrorEnvelope, "description": "rules document outside the document roots"},
    502: {"model": ErrorEnvelope, "description": "the model could not give a usable proposal"},
}
DECIDE_ERRORS = {
    404: {"model": ErrorEnvelope},
    422: {"model": ErrorEnvelope, "description": "missing or unknown target field"},
}


@router.post(
    "/projects/{project_id}/mappings/suggest",
    response_model=SuggestRunRead,
    summary="propose a target per source field: the model when configured, else the comparison",
    responses=SUGGEST_ERRORS,
)
def suggest(
    project_id: uuid.UUID, body: SuggestRequest, session: DbSession, provider: LlmProviderDep
) -> SuggestRunRead:
    project = get_project(session, project_id)
    return mapping_service.suggest_mappings(session, project, provider=provider, body=body)


@router.get(
    "/projects/{project_id}/mappings",
    response_model=list[FieldMappingRead],
    summary="every mapping, in source column order",
    responses=NOT_FOUND,
)
def list_mappings(
    project_id: uuid.UUID,
    session: DbSession,
    dataset: Annotated[str | None, Query()] = None,
    status: Annotated[MappingStatus | None, Query()] = None,
) -> list[FieldMappingRead]:
    project = get_project(session, project_id)
    rows = mapping_service.list_mappings(session, project, dataset=dataset, status=status)
    return [FieldMappingRead.model_validate(m) for m in rows]


@router.get(
    "/projects/{project_id}/mappings/summary",
    response_model=MappingSummary,
    summary="counts by status and dataset, open questions, required-field coverage",
    responses=NOT_FOUND,
)
def mapping_summary(project_id: uuid.UUID, session: DbSession) -> MappingSummary:
    project = get_project(session, project_id)
    return mapping_service.summary(session, project)


@router.post(
    "/projects/{project_id}/mappings/bulk-approve",
    response_model=BulkApproveResult,
    summary="approve suggested mappings at or above the high-confidence threshold",
    responses=NOT_FOUND,
)
def bulk_approve(
    project_id: uuid.UUID, body: BulkApproveRequest, session: DbSession
) -> BulkApproveResult:
    project = get_project(session, project_id)
    return mapping_service.bulk_approve(
        session, project, min_confidence=body.min_confidence, reviewer=body.reviewer
    )


@router.get(
    "/projects/{project_id}/mappings/{mapping_id}",
    response_model=FieldMappingRead,
    responses=NOT_FOUND,
)
def get_mapping(
    project_id: uuid.UUID, mapping_id: uuid.UUID, session: DbSession
) -> FieldMappingRead:
    project = get_project(session, project_id)
    return FieldMappingRead.model_validate(
        mapping_service.get_mapping(session, project, mapping_id)
    )


@router.patch(
    "/projects/{project_id}/mappings/{mapping_id}",
    response_model=FieldMappingRead,
    summary="approve, reject, ignore, edit, ask the customer, or reopen",
    responses=DECIDE_ERRORS,
)
def decide(
    project_id: uuid.UUID, mapping_id: uuid.UUID, body: MappingDecision, session: DbSession
) -> FieldMappingRead:
    project = get_project(session, project_id)
    return FieldMappingRead.model_validate(
        mapping_service.decide(session, project, mapping_id, body)
    )


@router.get(
    "/target-fields",
    response_model=list[dict],
    summary="the target catalog the mapping step lands fields in",
)
def target_fields() -> list[dict]:
    from app.target.catalog import target_field_catalog

    return [f.as_dict() for f in target_field_catalog()]


@router.get(
    "/projects/{project_id}/llm-calls",
    response_model=list[LlmCallRead],
    summary="every model call for this project: model, tokens, latency, cached, errors",
    responses=NOT_FOUND,
)
def llm_calls(project_id: uuid.UUID, session: DbSession) -> list[LlmCallRead]:
    project = get_project(session, project_id)
    return [LlmCallRead.model_validate(c) for c in mapping_service.list_llm_calls(session, project)]
