"""facts: everything already recorded about the project, read back. no new numbers are made here,
only counts of stored rows.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.models.mapping import ClarificationQuestion, QuestionStatus
from app.models.migration import MigrationRun, RunKind, RunStatus, ValidationIssueRow
from app.models.project import OnboardingProject
from app.models.target import WRITE_ORDER
from app.services import mapping as mapping_service
from app.services import reconciliation as reconciliation_service
from app.services.readiness.vocabulary import REPORT_VERSION, VOCABULARY_TYPES
from app.target.schema import PLATFORM_NAME


def _normalized(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _plan_state(session: Session, project: OnboardingProject, run: MigrationRun) -> dict[str, Any]:
    """does the dry run still describe what would run today? the plan is derived from the
    approved mappings and the configuration, so a changed decision or a new configuration
    version changes it."""
    from app.services.transform import build_plan

    try:
        current = build_plan(session, project).as_dict()
    except AppError as exc:
        return {"current": False, "reason": f"the plan cannot be built today: {exc.message}"}
    if _normalized(current) != _normalized(run.plan):
        then, now = run.config_version, current.get("config_version")
        reason = (
            f"the configuration changed from {then} to {now} since the rehearsal"
            if then != now
            else "mapping decisions changed since the rehearsal"
        )
        return {"current": False, "reason": reason}
    return {"current": True, "reason": None}


def _issue_groups(session: Session, run: MigrationRun) -> list[dict[str, Any]]:
    """the run's stored issues grouped by entity, severity and type, counted in records rather
    than issues (one record can carry two problems of the same kind)."""
    rows = session.execute(
        select(
            ValidationIssueRow.entity,
            ValidationIssueRow.severity,
            ValidationIssueRow.error_type,
            ValidationIssueRow.field,
            ValidationIssueRow.value,
            ValidationIssueRow.rule,
            ValidationIssueRow.dataset,
            ValidationIssueRow.source_row,
        ).where(ValidationIssueRow.run_id == run.id)
    ).all()
    records: dict[tuple[str, str, str], set[tuple[Any, Any]]] = defaultdict(set)
    fields: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    values: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    rules: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    for entity, severity, error_type, field_name, value, rule, dataset, source_row in rows:
        key = (entity, severity, error_type)
        records[key].add((dataset, source_row))
        if field_name:
            fields[key][field_name] += 1
        if rule:
            rules[key][rule] += 1
        if error_type in VOCABULARY_TYPES and value is not None:
            values[key][value] += 1
    groups = []
    for key, members in records.items():
        entity, severity, error_type = key
        groups.append(
            {
                "entity": entity,
                "severity": severity,
                "error_type": error_type,
                "records": len(members),
                "fields": [f for f, _ in fields[key].most_common(4)],
                "values": [[v, c] for v, c in values[key].most_common(8)],
                "rule": rules[key].most_common(1)[0][0] if rules[key] else None,
            }
        )
    order = {"error": 0, "warning": 1, "info": 2}
    groups.sort(key=lambda g: (order.get(g["severity"], 9), -g["records"], g["error_type"]))
    return groups


def _latest_dry_run(session: Session, project: OnboardingProject) -> MigrationRun | None:
    return session.execute(
        select(MigrationRun)
        .where(
            MigrationRun.project_id == project.id,
            MigrationRun.kind == RunKind.dry_run,
            MigrationRun.status == RunStatus.completed,
        )
        .order_by(MigrationRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def collect_facts(
    session: Session, project: OnboardingProject, settings: Settings | None = None
) -> dict[str, Any]:
    """everything the report says, read from what is stored. nothing here is estimated."""
    settings = settings or get_settings()
    datasets = []
    for d in sorted(project.datasets, key=lambda d: d.name):
        issues = (d.quality or {}).get("issue_counts", {})
        datasets.append(
            {
                "name": d.name,
                "kind": d.kind.value,
                "rows": d.row_count,
                "columns": d.column_count,
                "profiled": d.profiled_at is not None,
                "errors": int(issues.get("error", 0)),
                "warnings": int(issues.get("warning", 0)),
                "info": int(issues.get("info", 0)),
            }
        )

    summary = mapping_service.summary(session, project, settings)
    mappings = mapping_service.list_mappings(session, project)
    by_origin = Counter(m.origin.value for m in mappings if m.status.value == "approved")
    below_threshold = sum(
        1
        for m in mappings
        if m.status.value == "approved"
        and m.confidence is not None
        and m.confidence < settings.mapping_high_confidence
    )
    questions = list(
        session.execute(
            select(ClarificationQuestion)
            .where(
                ClarificationQuestion.project_id == project.id,
                ClarificationQuestion.status == QuestionStatus.open,
            )
            .order_by(ClarificationQuestion.created_at)
        ).scalars()
    )
    counts = summary.counts
    facts: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": {
            "id": str(project.id),
            "customer": project.customer_name,
            "name": project.project_name,
            "target_platform": PLATFORM_NAME,
            "target_environment": project.target_environment,
            "source_systems": list(project.source_systems or []),
            "stage": project.stage.value,
        },
        "data_profile": {
            "datasets": datasets,
            "rows": sum(d["rows"] or 0 for d in datasets if d["profiled"]),
            "profiled": sum(1 for d in datasets if d["profiled"]),
        },
        "mappings": {
            "total": summary.total,
            "approved": counts.get("approved", 0),
            "ignored": counts.get("ignored", 0),
            "rejected": counts.get("rejected", 0),
            "undecided": counts.get("suggested", 0) + counts.get("needs_clarification", 0),
            "open_questions": summary.open_questions,
            "approved_by_origin": dict(by_origin),
            "approved_below_high_confidence": below_threshold,
            "high_confidence_threshold": settings.mapping_high_confidence,
            "required_missing": sorted(
                path for c in summary.coverage for path in c.missing + c.pending
            ),
            # filled by the rehearsal's plan below: a field filled by a converter (one name
            # column split in two) has no mapping of its own and is not missing
        },
        "open_questions": [
            {
                "id": str(q.id),
                "dataset": q.dataset_name,
                "source_field": q.source_field,
                "question": q.question,
            }
            for q in questions
        ],
        "dry_run": None,
        "reconciliation": None,
        "issue_groups": [],
        "policy": {
            "min_entity_coverage": settings.readiness_min_entity_coverage,
        },
        "environment": {
            "target_fault_rate": settings.target_fault_rate,
        },
    }

    run = _latest_dry_run(session, project)
    if run is None:
        return facts
    options = run.options or {}
    partial = bool(options.get("limit_per_entity") or options.get("entities"))
    plan_state = _plan_state(session, project, run)
    facts["dry_run"] = {
        "id": str(run.id),
        "started_at": run.started_at.isoformat(timespec="seconds") if run.started_at else None,
        "duration_seconds": round((run.duration_ms or 0) / 1000, 1),
        "namespace": str(run.namespace) if run.namespace else None,
        "namespace_kept": not options.get("purge_namespace_after", False),
        "options": options,
        "partial": partial,
        "config_version": run.config_version,
        "as_of": run.as_of.isoformat() if run.as_of else None,
        "plan_current": plan_state["current"],
        "plan_stale_reason": plan_state["reason"],
        "stats": {e: run.stats[e] for e in WRITE_ORDER if e in (run.stats or {})},
        "totals": run.totals or {},
        "issue_counts": run.issue_counts or {},
        "applied_rules": run.applied_rules or {},
        "target_counts": run.target_counts or {},
        "issues_truncated": run.issues_truncated,
        "failures_truncated": run.failures_truncated,
    }
    facts["issue_groups"] = _issue_groups(session, run)
    emitted = {
        target
        for dataset in (run.plan or {}).get("datasets", [])
        for rule in dataset.get("field_rules", [])
        for target in rule.get("emits", [])
    }
    facts["mappings"]["required_missing"] = [
        path for path in facts["mappings"]["required_missing"] if path not in emitted
    ]

    reconciliation = reconciliation_service.latest_for_run(session, run)
    if reconciliation is not None:
        facts["reconciliation"] = {
            "id": str(reconciliation.id),
            "status": reconciliation.status.value,
            "trigger": reconciliation.trigger,
            "created_at": reconciliation.created_at.isoformat(timespec="seconds")
            if reconciliation.created_at
            else None,
            "entities": {
                e: reconciliation.entities[e] for e in WRITE_ORDER if e in reconciliation.entities
            },
            "totals": reconciliation.totals,
            "checks_passed": sum(1 for c in reconciliation.checks if c["ok"]),
            "checks_total": len(reconciliation.checks),
            "failed_checks": [c for c in reconciliation.checks if not c["ok"]],
            "discrepancies": reconciliation.discrepancies,
        }
    return facts
