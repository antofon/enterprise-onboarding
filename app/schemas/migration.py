"""request and response models for the transformation, validation and dry-run endpoints."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.migration import RunKind, RunStatus


class ValidateRequest(BaseModel):
    entities: list[str] | None = Field(
        default=None, description="only these target entities; default every entity in the plan"
    )
    limit_per_entity: int | None = Field(
        default=None,
        ge=1,
        description="stop after this many records per entity, for a quick look. Children whose "
        "organization falls past the limit are reported as blocked, not attempted",
    )


class DryRunRequest(ValidateRequest):
    stop_after_failures: int | None = Field(
        default=None, ge=1, description="give up on an entity after this many refusals"
    )
    purge_namespace_after: bool = Field(
        default=False,
        description="throw the staging records away when the run finishes instead of keeping "
        "them for inspection and reconciliation",
    )


class RunRead(BaseModel):
    """a validation pass or a dry run, with its counts."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    kind: RunKind
    status: RunStatus
    namespace: uuid.UUID | None = Field(
        default=None, description="the staging namespace in the target platform, for a dry run"
    )
    target_environment: str | None = None
    as_of: date | None
    config_version: str | None
    config: dict[str, Any] = {}
    options: dict[str, Any] = {}
    stats: dict[str, Any] = Field(
        default={}, description="per entity: source rows through to accepted and refused"
    )
    totals: dict[str, int] = {}
    issue_counts: dict[str, int] = Field(default={}, description="how many of each error type")
    applied_rules: dict[str, int] = Field(
        default={}, description="how many records each customer rule touched"
    )
    normalizations: dict[str, int] = Field(
        default={}, description="what normalization changed, and on how many values"
    )
    target_counts: dict[str, int] = Field(
        default={}, description="what the target platform holds in this run's namespace"
    )
    issues_truncated: bool = False
    failures_truncated: bool = False
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: float | None


class RunSummary(BaseModel):
    """one line per run, for a list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: RunKind
    status: RunStatus
    started_at: datetime
    duration_ms: float | None
    totals: dict[str, int] = {}
    namespace: uuid.UUID | None = None


class IssueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity: str
    dataset: str | None
    source_row: int | None
    record_id: str | None
    field: str | None
    error_type: str
    severity: str
    message: str
    value: str | None
    rule: str | None


class FailureRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity: str
    record_id: str | None
    dataset: str | None
    source_row: int | None
    stage: str
    attempts: int
    http_status: int | None
    error_type: str
    message: str
    request_id: str | None
    response_excerpt: str | None


class IssueBreakdownRow(BaseModel):
    entity: str
    severity: str
    error_type: str
    count: int
    fields: list[str] = []


class TransformationPlanRead(BaseModel):
    """the plan, derived from the approved mappings, before any row is touched."""

    customer: str
    config_version: str
    as_of: date
    entities: list[str]
    record_rules: list[str]
    cross_dataset_rules: list[str]
    value_maps: list[str]
    datasets: list[dict[str, Any]]
