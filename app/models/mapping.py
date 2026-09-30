"""field mappings, the questions we ask the customer about them, and every model call made."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import ONBOARDING_SCHEMA, Base

if TYPE_CHECKING:
    from app.models.project import OnboardingProject, SourceDataset


class MappingStatus(enum.StrEnum):
    """suggested: proposed, nobody has decided. needs_clarification: a question to the customer
    is open. approved / rejected / ignored: a person decided."""

    suggested = "suggested"
    needs_clarification = "needs_clarification"
    approved = "approved"
    rejected = "rejected"
    ignored = "ignored"


class MappingOrigin(enum.StrEnum):
    """who proposed the current target: the model, the deterministic comparison (manual mode),
    or a person."""

    model = "model"
    heuristic = "heuristic"
    manual = "manual"


class QuestionStatus(enum.StrEnum):
    open = "open"
    answered = "answered"
    withdrawn = "withdrawn"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(
        e, name=name, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x]
    )


class FieldMapping(Base):
    """one source column and where it lands in the target, with the trail of how that was
    decided. one row per column per project; re-suggesting replaces undecided rows only."""

    __tablename__ = "field_mappings"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "dataset_id", "source_field", name="uq_field_mapping_project_field"
        ),
        {"schema": ONBOARDING_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.projects.id", ondelete="CASCADE"), index=True
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.source_datasets.id", ondelete="CASCADE"), index=True
    )
    source_field: Mapped[str] = mapped_column(String(200))
    inferred_type: Mapped[str | None] = mapped_column(String(32))
    entity: Mapped[str | None] = mapped_column(String(32))
    target_path: Mapped[str | None] = mapped_column(String(100))

    status: Mapped[MappingStatus] = mapped_column(
        _enum(MappingStatus, "mapping_status"), default=MappingStatus.suggested, index=True
    )
    origin: Mapped[MappingOrigin] = mapped_column(_enum(MappingOrigin, "mapping_origin"))
    confidence: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    transformation_required: Mapped[bool] = mapped_column(Boolean, default=False)
    transformation: Mapped[str | None] = mapped_column(Text)
    clarification_required: Mapped[bool] = mapped_column(Boolean, default=False)

    # what the deterministic pass said, kept next to the proposal so the review screen can show
    # both without recomputing
    comparison_class: Mapped[str | None] = mapped_column(String(32))
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    model: Mapped[str | None] = mapped_column(String(100))
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project: Mapped[OnboardingProject] = relationship(back_populates="mappings")
    dataset: Mapped[SourceDataset] = relationship()
    questions: Mapped[list[ClarificationQuestion]] = relationship(
        back_populates="mapping", order_by="ClarificationQuestion.created_at"
    )

    @property
    def dataset_name(self) -> str:
        return self.dataset.name

    @property
    def open_question_count(self) -> int:
        return sum(1 for q in self.questions if q.status == QuestionStatus.open)

    @property
    def decided(self) -> bool:
        return self.status in (
            MappingStatus.approved,
            MappingStatus.rejected,
            MappingStatus.ignored,
        )


class ClarificationQuestion(Base):
    """a question for the customer, usually about one mapping. stored so it can be sent, answered
    in the tool, and shown on the readiness report while open."""

    __tablename__ = "clarification_questions"
    __table_args__ = {"schema": ONBOARDING_SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.projects.id", ondelete="CASCADE"), index=True
    )
    mapping_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.field_mappings.id", ondelete="SET NULL"), index=True
    )
    dataset_name: Mapped[str | None] = mapped_column(String(100))
    source_field: Mapped[str | None] = mapped_column(String(200))
    question: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    origin: Mapped[MappingOrigin] = mapped_column(_enum(MappingOrigin, "question_origin"))
    status: Mapped[QuestionStatus] = mapped_column(
        _enum(QuestionStatus, "question_status"), default=QuestionStatus.open, index=True
    )
    answer: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[str | None] = mapped_column(String(100))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped[OnboardingProject] = relationship(back_populates="questions")
    mapping: Mapped[FieldMapping | None] = relationship(back_populates="questions")


class LlmCall(Base):
    """every call to the model: what for, which model, what it cost, and the answer. the answer
    is the structured proposal (field names, reasons), never customer rows. `input_hash` is how
    a repeat of the same inputs is served from here instead of a new call."""

    __tablename__ = "llm_calls"
    __table_args__ = {"schema": ONBOARDING_SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.projects.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(50))
    dataset: Mapped[str | None] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(20))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    request_id: Mapped[str | None] = mapped_column(String(100))
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
