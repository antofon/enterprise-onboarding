"""run the plan over the customer's rows.

One pass per dataset: load the source, and for every row build one draft record per entity that
dataset emits. Each approved mapping's converter chain runs on its column, the record rules then
see the whole record, and the cross-dataset rules see everything. Nothing is written anywhere: the
source files are opened read-only and the result is held in memory for the validation stage.

What comes out is every record the migration would send, plus every reason a record cannot go and
every note about what normalization changed, which is what makes the dry run explainable rather
than just a number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.project import OnboardingProject
from app.models.target import ID_COLUMN
from app.services.profiling.loaders import load_source
from app.services.profiling.types import Severity
from app.services.transform import record_rules as rules
from app.services.transform.config import TransformationConfig, get_config
from app.services.transform.converters import NOTHING, ConverterContext, apply_chain
from app.services.transform.plan import DatasetPlan, FieldRule, TransformationPlan, build_plan
from app.services.transform.types import RecordDraft, RecordIssue
from app.target.catalog import catalog_by_path

log = get_logger(__name__)


@dataclass
class DatasetOutcome:
    dataset: str
    source_rows: int
    built: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "source_rows": self.source_rows,
            "built": self.built,
            "skipped": self.skipped,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass
class TransformResult:
    plan: TransformationPlan
    records: dict[str, list[RecordDraft]] = field(default_factory=dict)
    datasets: list[DatasetOutcome] = field(default_factory=list)
    cross_dataset: dict[str, dict[str, int]] = field(default_factory=dict)
    duration_ms: float = 0.0

    def all_records(self) -> list[RecordDraft]:
        return [r for entity in self.plan.entities for r in self.records.get(entity, [])]

    def issues(self) -> list[RecordIssue]:
        return [issue for record in self.all_records() for issue in record.issues]

    def counts(self) -> dict[str, dict[str, int]]:
        """per entity: how many records were built, skipped by a rule, and carry an error."""
        out: dict[str, dict[str, int]] = {}
        for entity in self.plan.entities:
            drafts = self.records.get(entity, [])
            skipped = sum(1 for d in drafts if d.skipped)
            errored = sum(1 for d in drafts if not d.skipped and d.errors)
            out[entity] = {
                "built": len(drafts),
                "skipped": skipped,
                "with_errors": errored,
                "transformed": len(drafts) - skipped - errored,
            }
        return out


def _row_values(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """rows as plain dicts, with pandas' NaN turned back into None."""
    records = frame.to_dict(orient="records")
    for row in records:
        for key, value in row.items():
            if value is None:
                continue
            if isinstance(value, float) and pd.isna(value):
                row[key] = None
            elif value is pd.NA or value is pd.NaT:
                row[key] = None
    return records


def _apply_field_rule(
    draft: RecordDraft,
    rule: FieldRule,
    raw: Any,
    ctx: ConverterContext,
) -> None:
    result = apply_chain(raw, rule.converters, ctx)
    for path in rule.emits:
        draft.sources[path] = rule.source_field
    if result.note:
        draft.note(rule.source_field, result.note)
    if result.failed:
        draft.add_issue(
            result.error_type,
            f"{rule.source_field}: {result.error}",
            severity=Severity.error,
            field_name=rule.target_field,
            value=raw,
        )
        return
    if result.value is NOTHING:
        return
    if isinstance(result.value, dict):
        # a converter that fills more than one target field, e.g. one name column into two
        for path in rule.emits:
            name = path.split(".", 1)[1]
            if name in result.value:
                draft.payload[name] = result.value[name]
        return
    draft.payload[rule.target_field] = result.value


def transform_project(
    session: Session,
    project: OnboardingProject,
    *,
    config: TransformationConfig | None = None,
    settings: Settings | None = None,
    http_client: httpx.Client | None = None,
    plan: TransformationPlan | None = None,
) -> TransformResult:
    settings = settings or get_settings()
    config = config or get_config()
    plan = plan or build_plan(session, project, config=config)
    catalog = catalog_by_path()
    started = time.perf_counter()

    contexts: dict[str, ConverterContext] = {}
    for dataset_plan in plan.datasets:
        for rule in dataset_plan.field_rules + dataset_plan.context_rules:
            target = catalog[rule.target_path]
            contexts[rule.target_path] = ConverterContext(
                config=config,
                target_path=rule.target_path,
                target_type=target.type,
                enum_values=target.enum_values,
                value_map=config.value_map(rule.target_path),
            )

    records: dict[str, list[RecordDraft]] = {entity: [] for entity in plan.entities}
    outcomes: list[DatasetOutcome] = []
    for dataset_plan in plan.datasets:
        if not dataset_plan.emits:
            continue
        outcomes.append(
            _transform_dataset(
                dataset_plan,
                records,
                config=config,
                settings=settings,
                http_client=http_client,
                contexts=contexts,
            )
        )

    cross: dict[str, dict[str, int]] = {}
    for ctx in rules.contexts_for(config, config.enabled_cross_dataset_rules()):
        fn = rules.CROSS_REGISTRY.get(ctx.rule_id)
        if fn is None:
            continue
        cross[ctx.rule_id] = fn(records, ctx)
        log.info("cross_dataset_rule", rule=ctx.rule_id, **cross[ctx.rule_id])

    result = TransformResult(
        plan=plan,
        records=records,
        datasets=outcomes,
        cross_dataset=cross,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    log.info(
        "transform_complete",
        duration_ms=result.duration_ms,
        **{entity: counts["transformed"] for entity, counts in result.counts().items()},
    )
    return result


def _transform_dataset(
    dataset_plan: DatasetPlan,
    records: dict[str, list[RecordDraft]],
    *,
    config: TransformationConfig,
    settings: Settings,
    http_client: httpx.Client | None,
    contexts: dict[str, ConverterContext],
) -> DatasetOutcome:
    started = time.perf_counter()
    frame = load_source(
        dataset_plan.kind, dataset_plan.location, settings=settings, http_client=http_client
    )
    rows = _row_values(frame)
    outcome = DatasetOutcome(dataset=dataset_plan.dataset, source_rows=len(rows))
    record_contexts = rules.contexts_for(config, config.enabled_record_rules())

    for entity in dataset_plan.emits:
        entity_rules = dataset_plan.rules_for(entity)
        id_field = ID_COLUMN[entity]
        built = skipped = 0
        for index, row in enumerate(rows, start=1):
            draft = RecordDraft(
                entity=entity, dataset=dataset_plan.dataset, source_row=index, raw=row
            )
            for rule in entity_rules:
                _apply_field_rule(
                    draft, rule, row.get(rule.source_field), contexts[rule.target_path]
                )
            # values mapped into an entity this dataset does not emit travel as context, for the
            # cross-dataset rules to use
            for rule in dataset_plan.context_rules:
                result = apply_chain(
                    row.get(rule.source_field), rule.converters, contexts[rule.target_path]
                )
                if not result.failed and result.value is not NOTHING:
                    draft.context[rule.target_path] = result.value
            if draft.payload.get(id_field) in (None, ""):
                draft.add_issue(
                    "missing_identifier",
                    f"no usable {id_field}: a record without an identifier cannot be migrated or "
                    "re-run safely",
                    severity=Severity.error,
                    field_name=id_field,
                )
            for ctx in record_contexts:
                fn = rules.REGISTRY.get(ctx.rule_id)
                if fn is not None:
                    fn(draft, ctx)
            records.setdefault(entity, []).append(draft)
            built += 1
            if draft.skipped:
                skipped += 1
        outcome.built[entity] = built
        outcome.skipped[entity] = skipped

    outcome.duration_ms = (time.perf_counter() - started) * 1000
    log.info(
        "dataset_transformed",
        dataset=dataset_plan.dataset,
        source_rows=outcome.source_rows,
        duration_ms=round(outcome.duration_ms, 1),
        **{f"built_{k}": v for k, v in outcome.built.items()},
    )
    return outcome


__all__ = ["DatasetOutcome", "TransformResult", "transform_project"]
