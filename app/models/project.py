"""the onboarding project and what we know about its source data."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
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
    from app.models.mapping import ClarificationQuestion, FieldMapping


class ProjectStage(enum.StrEnum):
    """where the project is in the lifecycle. moves forward as stages complete, and can
    fall back to in_review when a human reopens a mapping."""

    created = "created"
    profiled = "profiled"
    mapped = "mapped"
    in_review = "in_review"
    ready_to_transform = "ready_to_transform"
    validated = "validated"
    dry_run_complete = "dry_run_complete"
    reported = "reported"


class DatasetKind(enum.StrEnum):
    csv = "csv"
    json = "json"
    api = "api"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    # plain varchar, validated in python. native postgres enums fight create_all every time
    # a value is added, and the stage list will move during the build.
    return Enum(
        e, name=name, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x]
    )


class OnboardingProject(Base):
    __tablename__ = "projects"
    __table_args__ = {"schema": ONBOARDING_SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_name: Mapped[str] = mapped_column(String(200))
    project_name: Mapped[str] = mapped_column(String(200))
    source_systems: Mapped[list[str]] = mapped_column(JSONB, default=list)
    target_environment: Mapped[str] = mapped_column(String(50), default="staging")
    notes: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[ProjectStage] = mapped_column(
        _enum(ProjectStage, "project_stage"), default=ProjectStage.created
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    datasets: Mapped[list[SourceDataset]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="SourceDataset.name"
    )
    mappings: Mapped[list[FieldMapping]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    questions: Mapped[list[ClarificationQuestion]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class SourceDataset(Base):
    """one source file or feed belonging to a project, e.g. organizations.csv or the billing api."""

    __tablename__ = "source_datasets"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_source_dataset_project_name"),
        {"schema": ONBOARDING_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[DatasetKind] = mapped_column(_enum(DatasetKind, "dataset_kind"))
    location: Mapped[str] = mapped_column(String(500))
    row_count: Mapped[int | None] = mapped_column(Integer)
    column_count: Mapped[int | None] = mapped_column(Integer)
    profiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quality: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped[OnboardingProject] = relationship(back_populates="datasets")
    fields: Mapped[list[SourceField]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", order_by="SourceField.position"
    )


class SourceField(Base):
    """a column in a source dataset plus what profiling learned about it."""

    __tablename__ = "source_fields"
    __table_args__ = (
        UniqueConstraint("dataset_id", "name", name="uq_source_field_dataset_name"),
        {"schema": ONBOARDING_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{ONBOARDING_SCHEMA}.source_datasets.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(Integer)
    inferred_type: Mapped[str] = mapped_column(String(32))
    null_pct: Mapped[float] = mapped_column(Float, default=0.0)
    unique_pct: Mapped[float] = mapped_column(Float, default=0.0)
    distinct_count: Mapped[int] = mapped_column(Integer, default=0)
    sample_values: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    dataset: Mapped[SourceDataset] = relationship(back_populates="fields")
