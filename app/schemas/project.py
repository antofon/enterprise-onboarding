from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.project import DatasetKind, ProjectStage


def _clean(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


class ProjectCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200, examples=["Apex Equipment Services"])
    project_name: str = Field(min_length=1, max_length=200, examples=["Apex go-live migration"])
    source_systems: list[str] = Field(
        default_factory=list, examples=[["legacy crm export", "billing api", "business rules doc"]]
    )
    target_environment: str = Field(default="staging", max_length=50)
    notes: str | None = Field(default=None, max_length=5000)

    _strip = field_validator("customer_name", "project_name", "target_environment")(_clean)


class SourceFieldRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    position: int
    inferred_type: str
    null_pct: float
    unique_pct: float
    distinct_count: int
    sample_values: list[Any]
    stats: dict[str, Any]


class SourceDatasetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: DatasetKind
    location: str
    row_count: int | None
    column_count: int | None
    profiled_at: datetime | None
    quality: dict[str, Any] | None


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_name: str
    project_name: str
    source_systems: list[str]
    target_environment: str
    notes: str | None
    stage: ProjectStage
    created_at: datetime
    updated_at: datetime
    datasets: list[SourceDatasetRead] = []


class ProjectSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_name: str
    project_name: str
    stage: ProjectStage
    created_at: datetime
