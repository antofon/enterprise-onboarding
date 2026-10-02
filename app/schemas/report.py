"""request and response models for reconciliation and the readiness report."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.report import ReadinessStatus, ReconciliationStatus


class ReconciliationRead(BaseModel):
    """one reconciliation of one dry run. the identifier ledger is kept server side."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    trigger: str = Field(description="dry_run: made as the run finished. recheck: made later")
    status: ReconciliationStatus
    namespace: uuid.UUID | None
    entities: dict[str, Any] = Field(
        description="per entity: source rows through to what the target holds, the reasons "
        "records were excluded, and how the target answered"
    )
    totals: dict[str, int]
    checks: list[dict[str, Any]] = Field(description="every arithmetic and identity check")
    discrepancies: list[dict[str, Any]] = Field(
        description="the checks that failed, with sample identifiers"
    )
    target_counts: dict[str, int]
    created_at: datetime


class ReportRequest(BaseModel):
    use_model: bool = Field(
        default=True,
        description="let the configured model draft the executive summary from the facts. it "
        "is checked against them, and the summary is written by code when there is no model or "
        "the draft fails the check. the status and every number are computed either way",
    )
    generated_by: str | None = Field(default=None, max_length=100)


class ReportRead(BaseModel):
    """a readiness report. `content` holds the facts, the gates, the work, the questions, the
    next steps, the risks and the summary; the markdown export renders the same content."""

    id: uuid.UUID | None = Field(description="null for a preview that was not stored")
    project_id: uuid.UUID
    stored: bool
    status: ReadinessStatus
    summary_origin: str
    run_id: uuid.UUID | None = None
    reconciliation_id: uuid.UUID | None = None
    generated_by: str | None = None
    created_at: datetime | None = None
    content: dict[str, Any]


class ReportListRow(BaseModel):
    id: uuid.UUID
    status: ReadinessStatus
    summary_origin: str
    run_id: uuid.UUID | None
    blockers: int
    conditions: int
    created_at: datetime
