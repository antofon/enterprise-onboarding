from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field


class Severity(enum.StrEnum):
    """error: records will be rejected or cannot be migrated until someone decides.
    warning: a transformation or a decision is needed before the dry run.
    info: handled by normalization, listed so nobody is surprised."""

    error = "error"
    warning = "warning"
    info = "info"


class FieldProfile(BaseModel):
    """what profiling learned about one column. mirrors the source_fields table."""

    name: str
    position: int
    inferred_type: str
    null_count: int = 0
    null_pct: float = 0.0
    distinct_count: int = 0
    unique_pct: float = 0.0
    sample_values: list[str] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)


class QualityIssue(BaseModel):
    code: str
    severity: Severity
    column: str | None = None
    count: int
    pct: float | None = None
    message: str


class Reference(BaseModel):
    """a column in this dataset that carries another dataset's key."""

    column: str
    references: str
    checked: int
    resolved: int
    orphans: int
    orphan_pct: float
    orphan_examples: list[str] = Field(default_factory=list)


class DatasetProfile(BaseModel):
    name: str
    row_count: int
    column_count: int
    fields: list[FieldProfile]
    exact_duplicate_rows: int = 0
    key_column: str | None = None
    key_missing: int = 0
    key_duplicates: int = 0
    references: list[Reference] = Field(default_factory=list)
    issues: list[QualityIssue] = Field(default_factory=list)
    duration_ms: float = 0.0

    def field(self, name: str) -> FieldProfile:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def summary(self) -> dict[str, Any]:
        """the dataset-level part, what goes into source_datasets.quality."""
        return {**self.model_dump(exclude={"fields"}), "issue_counts": self.issue_counts()}

    def issue_counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for issue in self.issues:
            counts[issue.severity.value] += 1
        return counts
