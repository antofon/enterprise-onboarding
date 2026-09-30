from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.project import DatasetKind, ProjectStage
from app.schemas.project import SourceDatasetRead, SourceFieldRead


def _clean(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


class SourceAttach(BaseModel):
    name: str = Field(min_length=1, max_length=100, examples=["organizations.csv"])
    kind: DatasetKind
    location: str = Field(
        min_length=1,
        max_length=500,
        description="a file under a source root, or the url of a feed",
        examples=["sample_customer/data/organizations.csv"],
    )

    _strip = field_validator("name", "location")(_clean)


class AvailableSource(BaseModel):
    """something the api can attach: a file under a source root, or a known feed."""

    name: str
    kind: DatasetKind
    location: str
    size_bytes: int | None = None
    system: str | None = None


class SourceDatasetDetail(SourceDatasetRead):
    fields: list[SourceFieldRead] = []


class ProfiledDataset(BaseModel):
    id: uuid.UUID
    name: str
    kind: DatasetKind
    row_count: int
    column_count: int
    key_column: str | None
    issues: dict[str, int]
    duration_ms: float


class ProfileRunRead(BaseModel):
    project_id: uuid.UUID
    stage: ProjectStage
    profiled_at: datetime
    duration_ms: float
    datasets: list[ProfiledDataset]
