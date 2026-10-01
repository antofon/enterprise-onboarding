"""what the transformation stage produces: one draft record per source row per entity."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.profiling.types import Severity


@dataclass
class RecordIssue:
    """something wrong with one record, in the shape the validation_issues table stores.

    `error` means the record cannot be migrated as it stands. `warning` means it will migrate and
    somebody should look. `info` is normalization worth seeing, not a problem.
    """

    entity: str
    error_type: str
    message: str
    severity: Severity = Severity.error
    field: str | None = None
    value: str | None = None
    rule: str | None = None
    dataset: str | None = None
    source_row: int | None = None
    record_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "error_type": self.error_type,
            "message": self.message,
            "severity": self.severity.value,
            "field": self.field,
            "value": self.value,
            "rule": self.rule,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "record_id": self.record_id,
        }


@dataclass
class RecordDraft:
    """one target record on its way out of one source row.

    `payload` is the record as the target api will receive it. `raw` keeps the source row so
    record rules can read a column that has no target of its own (the dormancy date, the seat
    count behind a plan alias). `context` holds converted values for entities this dataset does
    not emit, which is how the account row's primary contact email reaches the contacts.
    """

    entity: str
    dataset: str
    source_row: int
    payload: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    applied_rules: list[str] = field(default_factory=list)
    issues: list[RecordIssue] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None
    skip_rule: str | None = None

    @property
    def record_id(self) -> str | None:
        from app.models.target import ID_COLUMN

        value = self.payload.get(ID_COLUMN[self.entity])
        return None if value is None else str(value)

    @property
    def errors(self) -> list[RecordIssue]:
        return [i for i in self.issues if i.severity is Severity.error]

    @property
    def valid(self) -> bool:
        return not self.skipped and not self.errors

    def note(self, field_name: str | None, text: str) -> None:
        self.notes.append(f"{field_name}: {text}" if field_name else text)

    def add_issue(
        self,
        error_type: str,
        message: str,
        *,
        severity: Severity = Severity.error,
        field_name: str | None = None,
        value: Any = None,
        rule: str | None = None,
    ) -> None:
        self.issues.append(
            RecordIssue(
                entity=self.entity,
                error_type=error_type,
                message=message,
                severity=severity,
                field=field_name,
                value=None if value is None else str(value)[:200],
                rule=rule,
                dataset=self.dataset,
                source_row=self.source_row,
                record_id=self.record_id,
            )
        )

    def skip(self, reason: str, rule: str | None = None) -> None:
        self.skipped = True
        self.skip_reason = reason
        self.skip_rule = rule

    def applied(self, rule: str) -> None:
        if rule not in self.applied_rules:
            self.applied_rules.append(rule)
