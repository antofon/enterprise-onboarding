from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.db import DbSession
from app.models.mapping import QuestionStatus
from app.schemas.common import ErrorEnvelope, Responses
from app.schemas.mapping import ClarificationQuestionRead, QuestionAnswer, QuestionCreate
from app.services import clarifications as service
from app.services.projects import get_project

router = APIRouter(tags=["clarifications"])

NOT_FOUND: Responses = {404: {"model": ErrorEnvelope}}
ANSWER_ERRORS: Responses = {
    404: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope, "description": "the question is not open"},
    422: {"model": ErrorEnvelope, "description": "resolution names an unknown target"},
}


@router.get(
    "/projects/{project_id}/clarifications",
    response_model=list[ClarificationQuestionRead],
    summary="questions for the customer, oldest first",
    responses=NOT_FOUND,
)
def list_questions(
    project_id: uuid.UUID,
    session: DbSession,
    status_filter: Annotated[QuestionStatus | None, Query(alias="status")] = None,
) -> list[ClarificationQuestionRead]:
    project = get_project(session, project_id)
    rows = service.list_questions(session, project, status=status_filter)
    return [ClarificationQuestionRead.model_validate(q) for q in rows]


@router.post(
    "/projects/{project_id}/clarifications",
    response_model=ClarificationQuestionRead,
    status_code=status.HTTP_201_CREATED,
    summary="ask the customer something, about a mapping or in general",
    responses=NOT_FOUND,
)
def create_question(
    project_id: uuid.UUID, body: QuestionCreate, session: DbSession
) -> ClarificationQuestionRead:
    project = get_project(session, project_id)
    return ClarificationQuestionRead.model_validate(service.create_question(session, project, body))


@router.get(
    "/projects/{project_id}/clarifications/{question_id}",
    response_model=ClarificationQuestionRead,
    responses=NOT_FOUND,
)
def get_question(
    project_id: uuid.UUID, question_id: uuid.UUID, session: DbSession
) -> ClarificationQuestionRead:
    project = get_project(session, project_id)
    return ClarificationQuestionRead.model_validate(
        service.get_question(session, project, question_id)
    )


@router.patch(
    "/projects/{project_id}/clarifications/{question_id}",
    response_model=ClarificationQuestionRead,
    summary="record the customer's answer; optionally resolve the mapping with it",
    responses=ANSWER_ERRORS,
)
def answer_question(
    project_id: uuid.UUID, question_id: uuid.UUID, body: QuestionAnswer, session: DbSession
) -> ClarificationQuestionRead:
    project = get_project(session, project_id)
    return ClarificationQuestionRead.model_validate(
        service.answer_question(session, project, question_id, body)
    )


@router.delete(
    "/projects/{project_id}/clarifications/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="withdraw an open question",
    responses={404: {"model": ErrorEnvelope}, 409: {"model": ErrorEnvelope}},
)
def withdraw_question(
    project_id: uuid.UUID,
    question_id: uuid.UUID,
    session: DbSession,
    by: Annotated[str, Query(max_length=100)] = "implementation engineer",
) -> Response:
    project = get_project(session, project_id)
    service.withdraw_question(session, project, question_id, by=by)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
