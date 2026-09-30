"""questions for the customer. asked by the model or by the engineer, answered in the tool, and
the answer can resolve the mapping on the spot."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import InvalidStateError, NotFoundError
from app.core.logging import get_logger
from app.models.mapping import (
    ClarificationQuestion,
    FieldMapping,
    MappingOrigin,
    MappingStatus,
    QuestionStatus,
)
from app.models.project import OnboardingProject
from app.schemas.mapping import MappingDecision, QuestionAnswer, QuestionCreate
from app.services import mapping as mapping_service

log = get_logger(__name__)


def _query(project: OnboardingProject):
    return (
        select(ClarificationQuestion)
        .where(ClarificationQuestion.project_id == project.id)
        .options(selectinload(ClarificationQuestion.mapping).selectinload(FieldMapping.questions))
        .order_by(ClarificationQuestion.created_at)
    )


def list_questions(
    session: Session, project: OnboardingProject, *, status: QuestionStatus | None = None
) -> list[ClarificationQuestion]:
    q = _query(project)
    if status:
        q = q.where(ClarificationQuestion.status == status)
    return list(session.execute(q).scalars())


def get_question(
    session: Session, project: OnboardingProject, question_id: uuid.UUID
) -> ClarificationQuestion:
    q = session.execute(
        _query(project).where(ClarificationQuestion.id == question_id)
    ).scalar_one_or_none()
    if q is None:
        raise NotFoundError(
            f"question {question_id} not found on this project",
            details={"project_id": str(project.id), "question_id": str(question_id)},
        )
    return q


def create_question(
    session: Session, project: OnboardingProject, body: QuestionCreate
) -> ClarificationQuestion:
    mapping = (
        mapping_service.get_mapping(session, project, body.mapping_id) if body.mapping_id else None
    )
    q = ClarificationQuestion(
        project_id=project.id,
        mapping=mapping,
        dataset_name=mapping.dataset.name if mapping else None,
        source_field=mapping.source_field if mapping else None,
        question=body.question.strip(),
        context={"asked_by": body.asked_by},
        origin=MappingOrigin.manual,
    )
    session.add(q)
    if mapping is not None:
        mapping.status = MappingStatus.needs_clarification
        mapping.clarification_required = True
        mapping.decided_by = None
        mapping.decided_at = None
    mapping_service.advance_stage(session, project)
    session.commit()
    session.refresh(q)
    log.info(
        "question_created",
        project_id=str(project.id),
        question_id=str(q.id),
        source_field=q.source_field,
    )
    return q


def answer_question(
    session: Session, project: OnboardingProject, question_id: uuid.UUID, body: QuestionAnswer
) -> ClarificationQuestion:
    q = get_question(session, project, question_id)
    if q.status != QuestionStatus.open:
        raise InvalidStateError(
            f"question is already {q.status.value}", details={"question_id": str(q.id)}
        )
    q.status = QuestionStatus.answered
    q.answer = body.answer.strip()
    q.answered_by = body.answered_by
    q.answered_at = datetime.now(UTC)
    q.resolution = body.resolution.model_dump() if body.resolution else None
    session.flush()

    mapping = q.mapping
    if mapping is not None:
        if body.resolution is not None:
            mapping_service.decide(
                session,
                project,
                mapping.id,
                MappingDecision(
                    action=body.resolution.action,
                    target_path=body.resolution.target_path,
                    transformation=body.resolution.transformation,
                    note=f"customer answered: {q.answer[:300]}",
                    reviewer=body.answered_by,
                ),
            )
        else:
            still_open = [
                x for x in mapping.questions if x.status == QuestionStatus.open and x.id != q.id
            ]
            if not still_open:
                # back to the engineer's queue with the answer attached
                mapping.status = MappingStatus.suggested
                mapping.clarification_required = False
                mapping.decision_note = f"customer answered: {q.answer[:300]}"
    mapping_service.advance_stage(session, project)
    session.commit()
    session.refresh(q)
    log.info(
        "question_answered",
        project_id=str(project.id),
        question_id=str(q.id),
        resolved=body.resolution is not None,
    )
    return q


def withdraw_question(
    session: Session, project: OnboardingProject, question_id: uuid.UUID, *, by: str
) -> None:
    q = get_question(session, project, question_id)
    if q.status != QuestionStatus.open:
        raise InvalidStateError(
            f"question is already {q.status.value}", details={"question_id": str(q.id)}
        )
    q.status = QuestionStatus.withdrawn
    q.resolution = {"withdrawn": f"by {by}"}
    mapping = q.mapping
    if mapping is not None:
        others = [x for x in mapping.questions if x.status == QuestionStatus.open and x.id != q.id]
        if not others and mapping.status == MappingStatus.needs_clarification:
            mapping.status = MappingStatus.suggested
            mapping.clarification_required = False
    mapping_service.advance_stage(session, project)
    session.commit()
    log.info("question_withdrawn", project_id=str(project.id), question_id=str(q.id))
