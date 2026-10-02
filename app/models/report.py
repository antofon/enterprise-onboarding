"""reconciliation results and readiness reports: the two things a dry run is for.

A reconciliation is arithmetic over one dry run: every source row followed through built,
skipped, excluded, sent, refused and accepted, and the accepted identifiers compared one by one
with what the target platform actually holds in the run's namespace. It is stored because the
target can change after the run (a namespace purged, a record written by somebody else), and a
re-check has to be compared with what the run saw.

A readiness report is the answer to "can this customer go live": a status decided by stated
rules over stored facts, the work that stands between the customer and go-live, and a summary
for people who will not read the appendix. The facts and the rendered markdown are both kept, so
the report a customer was sent can be read back exactly as it was.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import ONBOARDING_SCHEMA, Base

if TYPE_CHECKING:
    from app.models.migration import MigrationRun


class ReconciliationStatus(enum.StrEnum):
    """balanced: every count adds up and the target holds exactly the records the run accepted.
    discrepancies: at least one check failed, and the result says which."""

    balanced = "balanced"
    discrepancies = "discrepancies"


class ReadinessStatus(enum.StrEnum):
    ready = "READY"
    ready_with_conditions = "READY WITH CONDITIONS"
    blocked = "BLOCKED"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(
        e, name=name, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x]
    )


class ReconciliationResult(Base):
    """one reconciliation of one dry run, at the end of the run or on a later re-check."""

    __tablename__ = "reconciliation_results"
    __table_args__ = {"schema": ONBOARDING_SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.migration_runs.id", ondelete="CASCADE"), index=True
    )
    # dry_run: computed as the run finished. recheck: the target read again later
    trigger: Mapped[str] = mapped_column(String(20))
    status: Mapped[ReconciliationStatus] = mapped_column(
        _enum(ReconciliationStatus, "reconciliation_status")
    )
    namespace: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # per entity: the stage counts from source rows to what the target holds, the reasons records
    # were excluded, and how the target answered
    entities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    totals: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # every arithmetic and identity check, passed or not
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # the checks that failed, with sample identifiers so somebody can go and look
    discrepancies: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # the identifiers the run saw accepted, per entity. the customer's ids, not their rows; this
    # is what a re-check compares the target against
    ledger: Mapped[dict[str, list[str]]] = mapped_column(JSONB, default=dict)
    target_counts: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[MigrationRun] = relationship()


class ReadinessReport(Base):
    """one generated readiness report. never updated: a new report is a new row."""

    __tablename__ = "readiness_reports"
    __table_args__ = {"schema": ONBOARDING_SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.migration_runs.id", ondelete="SET NULL")
    )
    reconciliation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.reconciliation_results.id", ondelete="SET NULL")
    )
    status: Mapped[ReadinessStatus] = mapped_column(_enum(ReadinessStatus, "readiness_status"))
    # the whole report as json: facts, gates, work items, questions, next steps, risks, summary
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    markdown: Mapped[str] = mapped_column(Text)
    # model: drafted by the configured model and checked against the facts. template: written
    # from the facts by code, because no model is configured or its draft failed the check
    summary_origin: Mapped[str] = mapped_column(String(20))
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.llm_calls.id", ondelete="SET NULL")
    )
    generated_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
