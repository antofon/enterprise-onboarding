"""what a validation pass or a dry run actually did, kept so it can be read afterwards.

A run row is the audit trail: which configuration version, which plan, how many records of each
entity were built, skipped, rejected and accepted, how long it took, and what went wrong. The
issues and the target failures hang off it.

What is deliberately not stored: the customer's rows. An issue keeps the identifier, the column and
the offending value, which is what somebody needs to go and look, and nothing more.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import ONBOARDING_SCHEMA, Base

if TYPE_CHECKING:
    from app.models.project import OnboardingProject


class RunKind(enum.StrEnum):
    """validation stops after checking. dry_run goes on to write into a staging namespace."""

    validation = "validation"
    dry_run = "dry_run"


class RunStatus(enum.StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"


class FailureStage(enum.StrEnum):
    """where a record fell over: refused by the target, or never attempted because the
    organization it belongs to was refused first."""

    target = "target"
    blocked = "blocked"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(
        e, name=name, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x]
    )


class MigrationRun(Base):
    """one validation pass or one dry run."""

    __tablename__ = "migration_runs"
    __table_args__ = {"schema": ONBOARDING_SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[RunKind] = mapped_column(_enum(RunKind, "run_kind"), index=True)
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), default=RunStatus.running, index=True
    )
    # the staging namespace in the target platform. null for a validation pass, which writes nothing
    namespace: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    target_environment: Mapped[str | None] = mapped_column(String(50))

    as_of: Mapped[date | None] = mapped_column(Date)
    config_version: Mapped[str | None] = mapped_column(String(50))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # per entity: built, skipped_by_rule, valid, invalid, attempted, accepted, rejected, failed
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    totals: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    issue_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    applied_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    normalizations: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    target_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    issues_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    failures_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)

    project: Mapped[OnboardingProject] = relationship()
    issues: Mapped[list[ValidationIssueRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    failures: Mapped[list[MigrationFailure]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class ValidationIssueRow(Base):
    """one thing wrong with one record, from the transformation or the validation stage."""

    __tablename__ = "validation_issues"
    __table_args__ = {"schema": ONBOARDING_SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.migration_runs.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.projects.id", ondelete="CASCADE"), index=True
    )
    entity: Mapped[str] = mapped_column(String(32), index=True)
    dataset: Mapped[str | None] = mapped_column(String(100))
    source_row: Mapped[int | None] = mapped_column(Integer)
    record_id: Mapped[str | None] = mapped_column(String(64), index=True)
    field: Mapped[str | None] = mapped_column(String(100))
    error_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    message: Mapped[str] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(String(300))
    rule: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[MigrationRun] = relationship(back_populates="issues")


class MigrationFailure(Base):
    """a record the target refused, or never saw because its organization was refused first."""

    __tablename__ = "migration_failures"
    __table_args__ = {"schema": ONBOARDING_SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.migration_runs.id", ondelete="CASCADE"), index=True
    )
    entity: Mapped[str] = mapped_column(String(32), index=True)
    record_id: Mapped[str | None] = mapped_column(String(64), index=True)
    dataset: Mapped[str | None] = mapped_column(String(100))
    source_row: Mapped[int | None] = mapped_column(Integer)
    stage: Mapped[FailureStage] = mapped_column(_enum(FailureStage, "failure_stage"))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(100))
    response_excerpt: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[MigrationRun] = relationship(back_populates="failures")
