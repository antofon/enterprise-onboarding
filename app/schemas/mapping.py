from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.mapping import MappingOrigin, MappingStatus, QuestionStatus
from app.models.project import ProjectStage

# --- suggesting -------------------------------------------------------------------------------


class SuggestRequest(BaseModel):
    datasets: list[str] | None = Field(
        default=None, description="dataset names to (re)suggest; default every profiled dataset"
    )
    rules_document: str | None = Field(
        default=None,
        description="the customer's business rules, a file under a document root",
        examples=["sample_customer/business_rules.md"],
    )
    force: bool = Field(
        default=False,
        description="replace decided mappings and re-ask open questions; also skips the cache",
    )


class DatasetSuggestResult(BaseModel):
    dataset: str
    entity: str | None
    origin: MappingOrigin
    written: int = Field(description="mappings created or replaced")
    kept: int = Field(description="mappings a person had already decided, left alone")
    questions: int = Field(description="clarification questions opened")
    cached: bool
    llm_call_id: uuid.UUID | None
    observations: list[str] = []


class SuggestRunRead(BaseModel):
    project_id: uuid.UUID
    stage: ProjectStage
    provider: str
    model: str | None
    prompt_version: str
    datasets: list[DatasetSuggestResult]
    counts: dict[str, int]
    duration_ms: float


# --- reading ----------------------------------------------------------------------------------


class FieldMappingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    dataset_id: uuid.UUID
    dataset_name: str
    source_field: str
    inferred_type: str | None
    entity: str | None
    target_path: str | None
    status: MappingStatus
    origin: MappingOrigin
    confidence: float | None
    reason: str | None
    transformation_required: bool
    transformation: str | None
    clarification_required: bool
    comparison_class: str | None
    candidates: list[dict[str, Any]]
    model: str | None
    llm_call_id: uuid.UUID | None
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    open_question_count: int
    created_at: datetime
    updated_at: datetime


class EntityMappingCoverage(BaseModel):
    entity: str
    required: list[str]
    approved: list[str]
    pending: list[str] = Field(description="proposed but not yet decided")
    missing: list[str] = Field(description="no approved or pending mapping lands here")


class MappingSummary(BaseModel):
    project_id: uuid.UUID
    stage: ProjectStage
    total: int
    counts: dict[str, int]
    by_dataset: dict[str, dict[str, int]]
    open_questions: int
    high_confidence_pending: int = Field(description="suggested, target set, confidence >= high")
    low_confidence_pending: int = Field(description="suggested or asking, confidence < low")
    thresholds: dict[str, float]
    coverage: list[EntityMappingCoverage]


# --- deciding ---------------------------------------------------------------------------------

Action = Literal["approve", "reject", "ignore", "edit", "clarify", "reopen"]


class MappingDecision(BaseModel):
    action: Action
    target_path: str | None = Field(
        default=None, description="entity.field from the catalog; approve/edit may set it"
    )
    transformation: str | None = Field(
        default=None, description="the deterministic rule in plain english; empty string clears"
    )
    note: str | None = Field(default=None, max_length=2000)
    reviewer: str = Field(default="implementation engineer", max_length=100)
    question: str | None = Field(
        default=None, max_length=4000, description="clarify: the question to send the customer"
    )

    @model_validator(mode="after")
    def _edit_needs_something(self) -> MappingDecision:
        if self.action == "edit" and self.target_path is None and self.transformation is None:
            raise ValueError("edit needs a target_path or a transformation")
        return self


class BulkApproveRequest(BaseModel):
    min_confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description=(
            "approve suggested mappings at or above this; never below the configured high threshold"
        ),
    )
    reviewer: str = Field(default="implementation engineer", max_length=100)


class BulkApproveResult(BaseModel):
    approved: int
    skipped: int = Field(description="suggested mappings with a target that stayed pending")
    min_confidence: float
    stage: ProjectStage


# --- clarification questions ------------------------------------------------------------------


class QuestionCreate(BaseModel):
    mapping_id: uuid.UUID | None = None
    question: str = Field(min_length=1, max_length=4000)
    asked_by: str = Field(default="implementation engineer", max_length=100)


class QuestionResolution(BaseModel):
    action: Literal["approve", "ignore", "reject"]
    target_path: str | None = None
    transformation: str | None = None


class QuestionAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=8000)
    answered_by: str = Field(default="customer", max_length=100)
    resolution: QuestionResolution | None = Field(
        default=None,
        description="apply the answer to the mapping right away; leave empty to decide later",
    )


class ClarificationQuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    mapping_id: uuid.UUID | None
    dataset_name: str | None
    source_field: str | None
    question: str
    context: dict[str, Any]
    origin: MappingOrigin
    status: QuestionStatus
    answer: str | None
    answered_by: str | None
    answered_at: datetime | None
    resolution: dict[str, Any] | None
    created_at: datetime


# --- model calls ------------------------------------------------------------------------------


class LlmCallRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID | None
    purpose: str
    dataset: str | None
    provider: str
    model: str
    prompt_version: str
    input_hash: str
    cached: bool
    status: str
    attempts: int
    error: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: float | None
    request_id: str | None
    created_at: datetime


# --- documents --------------------------------------------------------------------------------


class AvailableDocument(BaseModel):
    """a context document the mapping step can read: business rules, kickoff notes."""

    name: str
    location: str
    size_bytes: int
