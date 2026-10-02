"""the implementation readiness report: can this customer go live, and if not, what first.

Four layers, and only the last one may involve a model:

  facts        everything already recorded about the project, read back: the profile, the
               mapping decisions, the latest dry run, its reconciliation, every issue it found.
               no new numbers are made here, only counts of stored rows.
  gates        stated rules over the facts decide the status. any blocker is BLOCKED, any
               condition is READY WITH CONDITIONS, otherwise READY. each gate says what it
               checked, what it found, and which threshold it used, so a status can be argued
               with line by line.
  work         the issues grouped into what somebody has to do: decisions only the customer can
               make, data the customer has to correct, records that wait on another fix, and
               warnings to sign off. customer questions, next steps and technical risks follow
               from the work and the gates by rule.
  summary      a page a sponsor will read. drafted by the configured model from the facts and
               checked: the status must be stated as decided, every number must appear in the
               facts, every explanation must name a real work item. a draft that fails is sent
               back with the problems; after the last attempt, or with no model configured, the
               summary is written from the facts by code, and the report says which.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.provider import LlmError, LlmProvider, LlmUnavailableError, StructuredResult
from app.ai.report_prompt import REPORT_PROMPT_VERSION, build_summary_prompt
from app.ai.schemas import ReadinessSummary
from app.core.config import Settings, get_settings
from app.core.errors import AppError, NotFoundError
from app.core.logging import get_logger, stage
from app.models.mapping import ClarificationQuestion, LlmCall, QuestionStatus
from app.models.migration import MigrationRun, RunKind, RunStatus, ValidationIssueRow
from app.models.project import OnboardingProject
from app.models.report import ReadinessReport, ReadinessStatus, ReconciliationStatus
from app.models.target import WRITE_ORDER
from app.services import mapping as mapping_service
from app.services import reconciliation as reconciliation_service
from app.services import workflow
from app.services.workflow import Event
from app.target.schema import PLATFORM_NAME

log = get_logger(__name__)

REPORT_VERSION = "1"
PURPOSE_SUMMARY = "readiness_summary"
BLOCKER = "blocker"
CONDITION = "condition"
PASS = "pass"
PLURAL = {
    "organization": "organizations",
    "contact": "contacts",
    "subscription": "subscriptions",
    "activity": "activities",
}
# issue types whose offending values are vocabulary, safe to quote in a report. emails, phones,
# names and whole records are personal data and are never quoted, only counted
VOCABULARY_TYPES = {
    "unmapped_value",
    "invalid_enum_value",
    "undecidable_plan",
    "ambiguous_country",
    "plan_conflicts_with_tier",
}
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_STATUS_WORDS = ("READY WITH CONDITIONS", "READY", "BLOCKED")


# --- what each kind of issue asks of somebody ----------------------------------------------------


@dataclass(frozen=True)
class Action:
    """how one kind of issue becomes work. `kind` decides where it lands in the report."""

    kind: str  # decision | data_fix | dependent | review
    title: str
    question: str | None = None


ACTIONS: dict[str, Action] = {
    "unmapped_value": Action(
        "decision",
        "{n} {entities} carry {field} values that no rule maps ({values})",
        "Which {platform} value should {values_plain} in {field} become? Until a rule says so, "
        "{these} {n} {entities} stay behind.",
    ),
    "invalid_enum_value": Action(
        "decision",
        "{n} {entities} carry {field} values {platform} does not accept ({values})",
        "What should {values_plain} in {field} become in {platform}?",
    ),
    "undecidable_plan": Action(
        "decision",
        "{n} {entities} cannot be given a {field}: {values} depends on a value that is empty",
        "Which plan should {these} {n} {entities} get? The rule that decides {values_plain} "
        "needs a value the export does not have.",
    ),
    "business_rule_violation": Action(
        "decision",
        "{n} {entities} conflict with a {platform} rule ({rule})",
        "{These} {n} {entities} conflict with a {platform} rule ({rule}). How should each one "
        "be resolved before migration?",
    ),
    "plan_conflicts_with_tier": Action(
        "decision",
        "{n} {entities} are on a plan the customer's own rules say they should not have",
        "{n} {entities} sit on accounts whose tier calls for a different plan ({rule}). Is the "
        "billing plan right, or should these accounts move plan?",
    ),
    "ambiguous_country": Action(
        "decision",
        "{n} {entities} have country {values} and nothing that tells which country it is",
        "Which country are {these} {n} {entities} in? The export says {values_plain} and has "
        "no state or region to decide it.",
    ),
    "missing_identifier": Action("data_fix", "{n} {entities} have no identifier"),
    "duplicate_identifier": Action(
        "data_fix", "{n} {entities} reuse an identifier that is already in the export"
    ),
    "missing_required": Action(
        "data_fix", "{n} {entities} are missing values {platform} requires ({fields})"
    ),
    "malformed_email": Action("data_fix", "{n} {entities} have an email address that is not valid"),
    "phone_too_short": Action("data_fix", "{n} {entities} have a phone number with too few digits"),
    "duplicate_contact_email": Action(
        "data_fix", "{n} {entities} repeat an email address already used on the same account"
    ),
    "duplicate_primary_contact": Action(
        "data_fix", "{n} {entities} are marked primary on accounts that already have one"
    ),
    "missing_relationship": Action(
        "data_fix", "{n} {entities} belong to accounts that are not in the export at all"
    ),
    "invalid_value": Action("data_fix", "{n} {entities} fail a value check {platform} applies"),
    "invalid_date": Action("data_fix", "{n} {entities} have dates that cannot be read ({fields})"),
    "invalid_format": Action(
        "data_fix", "{n} {entities} have values in a format {platform} refuses ({fields})"
    ),
    "parent_record_rejected": Action(
        "dependent",
        "{n} {entities} wait on an account that cannot migrate yet; fixing the account "
        "releases them",
    ),
    "no_primary_contact": Action(
        "review", "{n} {entities} belong to accounts that have no primary contact ({rule})"
    ),
    "primary_contact_not_found": Action(
        "review",
        "{n} {entities} name a primary contact that the contact export does not contain",
    ),
    "primary_contact_missing": Action(
        "review", "{n} {entities} name a primary contact and have no contacts in the export"
    ),
    "date_in_future": Action(
        "review", "{n} {entities} have dates after the migration date ({fields})"
    ),
}
# a platform rule the validation stage names, and the question it puts to the customer. keyed by
# a phrase from the rule text, which is this tool's own vocabulary, not a customer's
RULE_QUESTIONS: dict[str, str] = {
    "dormant account": (
        "{These} {n} {entities} are live in billing, but the account each one belongs to "
        "migrates as inactive, and {platform} allows no live subscription under an inactive "
        "account. For each account: should it stay active in {platform}, or should the "
        "subscription end before migration?"
    ),
}
FALLBACK_ERROR = Action("data_fix", "{n} {entities}: {error_type} on {fields}")
FALLBACK_WARNING = Action("review", "{n} {entities}: {error_type} on {fields}")
OWNERS = {
    "decision": "customer decision",
    "data_fix": "customer data correction",
    "dependent": "released by other fixes",
    "review": "review and sign-off",
}


def _fmt(n: int) -> str:
    return f"{n:,}"


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "n/a"


def _plain_values(values: list[tuple[str, int]]) -> str:
    shown = [f"'{v}'" for v, _ in values[:4]]
    if len(values) > 4:
        return ", ".join(shown) + f" and {len(values) - 4} more"
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + f" or {shown[-1]}"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _when(iso: str | None) -> str:
    """2026-10-02T07:38:31+00:00 -> 2026-10-02 07:38 UTC"""
    return f"{iso[:10]} {iso[11:16]} UTC" if iso and len(iso) >= 16 else (iso or "")


def _quote_values(values: list[tuple[str, int]]) -> str:
    shown = [f"'{v}' ({_fmt(c)})" for v, c in values[:4]]
    more = len(values) - 4
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


# --- facts ---------------------------------------------------------------------------------------


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


# --- gates ---------------------------------------------------------------------------------------


def _gate(code: str, outcome: str, title: str, detail: str) -> dict[str, Any]:
    return {"code": code, "outcome": outcome, "title": title, "detail": _cap(detail)}


def assess(facts: dict[str, Any]) -> tuple[ReadinessStatus, list[dict[str, Any]]]:
    """the status, decided by stated rules. pure: the unit tests drive every branch."""
    gates: list[dict[str, Any]] = []
    m = facts["mappings"]
    if m["undecided"] or m["open_questions"]:
        gates.append(
            _gate(
                "mappings_decided",
                BLOCKER,
                "Mapping decisions are not finished",
                f"{_fmt(m['undecided'])} fields have no decision and {_fmt(m['open_questions'])} "
                "customer questions are open. Nothing can be transformed until every field is "
                "decided.",
            )
        )
    else:
        gates.append(
            _gate(
                "mappings_decided",
                PASS,
                "Every field is decided",
                f"{_fmt(m['approved'])} approved, {_fmt(m['ignored'])} not migrated, "
                f"{_fmt(m['rejected'])} rejected, no open questions.",
            )
        )

    run = facts["dry_run"]
    if run is None:
        gates.append(
            _gate(
                "rehearsal",
                BLOCKER,
                "The migration has not been rehearsed",
                "No completed dry run exists. Readiness is judged on a rehearsal against the "
                "target, not on validation alone.",
            )
        )
        return _status(gates), gates

    if run["partial"]:
        gates.append(
            _gate(
                "rehearsal",
                BLOCKER,
                "The latest rehearsal covered part of the data",
                f"The dry run was limited ({json.dumps(run['options'])}). Rehearse the whole "
                "migration before judging readiness.",
            )
        )
    else:
        gates.append(
            _gate(
                "rehearsal",
                PASS,
                "The whole migration was rehearsed",
                f"dry run {run['id'][:8]} sent every valid record to the target in "
                f"{run['duration_seconds']} seconds.",
            )
        )
    if not run["plan_current"]:
        gates.append(
            _gate(
                "rehearsal_current",
                BLOCKER,
                "The rehearsal no longer matches the plan",
                f"{run['plan_stale_reason']}. Rehearse again so the numbers describe what would "
                "actually run.",
            )
        )
    else:
        gates.append(
            _gate(
                "rehearsal_current",
                PASS,
                "The rehearsal matches today's plan",
                f"same approved mappings and configuration {run['config_version']}.",
            )
        )

    rec = facts["reconciliation"]
    if rec is None:
        gates.append(
            _gate(
                "reconciliation",
                BLOCKER,
                "The rehearsal has not been reconciled",
                "No reconciliation is recorded for this dry run. Rehearse again.",
            )
        )
    elif rec["status"] != ReconciliationStatus.balanced.value:
        kinds = ", ".join(sorted({d["kind"] for d in rec["discrepancies"]}))
        gates.append(
            _gate(
                "reconciliation",
                BLOCKER,
                "Source and target do not reconcile",
                f"{_fmt(rec['checks_total'] - rec['checks_passed'])} of "
                f"{_fmt(rec['checks_total'])} checks failed ({kinds}). Every number has to be "
                "explained before go-live.",
            )
        )
    else:
        gates.append(
            _gate(
                "reconciliation",
                PASS,
                "Source and target reconcile",
                f"all {_fmt(rec['checks_total'])} checks passed: every source row is accounted "
                "for and the target holds exactly the accepted records.",
            )
        )

    totals = run["totals"]
    refused = int(totals.get("rejected", 0)) + int(totals.get("failed", 0))
    if refused:
        gates.append(
            _gate(
                "target_accepted",
                BLOCKER,
                "The target refused or failed records that passed validation",
                f"{_fmt(int(totals.get('rejected', 0)))} refused and "
                f"{_fmt(int(totals.get('failed', 0)))} failed of "
                f"{_fmt(int(totals.get('attempted', 0)))} sent. "
                "Validation should catch everything the target refuses; a refusal is a "
                "gap in the checks or a fault in the target.",
            )
        )
    else:
        gates.append(
            _gate(
                "target_accepted",
                PASS,
                "The target accepted every record sent",
                f"{_fmt(int(totals.get('accepted', 0)))} of "
                f"{_fmt(int(totals.get('attempted', 0)))} accepted.",
            )
        )

    floor = facts["policy"]["min_entity_coverage"]
    for entity, stats in run["stats"].items():
        in_scope = int(stats.get("built", 0)) - int(stats.get("skipped_by_rule", 0))
        accepted = int(stats.get("accepted", 0))
        share = accepted / in_scope if in_scope else 1.0
        left = in_scope - accepted
        plural = PLURAL.get(entity, entity)
        detail = (
            f"{_fmt(accepted)} of {_fmt(in_scope)} {plural} in scope landed "
            f"({_pct(accepted, in_scope)}); {_fmt(left)} stay behind."
        )
        if share < floor:
            gates.append(
                _gate(
                    f"coverage:{entity}",
                    BLOCKER,
                    f"Too many {plural} cannot migrate",
                    detail + f" The floor is {floor * 100:.0f}%.",
                )
            )
        elif left:
            gates.append(
                _gate(
                    f"coverage:{entity}",
                    CONDITION,
                    f"Some {plural} cannot migrate",
                    detail + " The customer fixes them or signs off on leaving them behind.",
                )
            )
        else:
            gates.append(
                _gate(f"coverage:{entity}", PASS, f"Every {entity} in scope landed", detail)
            )

    warned = int(totals.get("with_warnings", 0))
    if warned:
        gates.append(
            _gate(
                "warnings",
                CONDITION,
                "Records will migrate with warnings",
                f"{_fmt(warned)} records pass but carry a warning somebody has to read and "
                "accept before go-live.",
            )
        )
    else:
        gates.append(_gate("warnings", PASS, "No warnings", "no record carries a warning."))
    return _status(gates), gates


def _status(gates: list[dict[str, Any]]) -> ReadinessStatus:
    outcomes = {g["outcome"] for g in gates}
    if BLOCKER in outcomes:
        return ReadinessStatus.blocked
    if CONDITION in outcomes:
        return ReadinessStatus.ready_with_conditions
    return ReadinessStatus.ready


# --- work ----------------------------------------------------------------------------------------


def work_items(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """issues grouped into what somebody has to do, largest first within each kind."""
    items = []
    for group in facts["issue_groups"]:
        error = group["severity"] == "error"
        action = ACTIONS.get(group["error_type"]) or (FALLBACK_ERROR if error else FALLBACK_WARNING)
        values = [(v, c) for v, c in group["values"]]
        params = {
            "n": _fmt(group["records"]),
            "entities": PLURAL.get(group["entity"], group["entity"])
            if group["records"] != 1
            else group["entity"],
            "field": "/".join(group["fields"][:1]) or "the value",
            "fields": ", ".join(group["fields"]) or "no single field",
            "values": _quote_values(values) if values else "these values",
            "values_plain": _plain_values(values) if values else "these values",
            "these": "this" if group["records"] == 1 else "these",
            "These": "This" if group["records"] == 1 else "These",
            "rule": group["rule"] or "no rule recorded",
            "platform": PLATFORM_NAME,
            "error_type": group["error_type"],
        }
        items.append(
            {
                "code": f"{group['entity']}:{group['error_type']}",
                "kind": action.kind,
                "owner": OWNERS[action.kind],
                "severity": group["severity"],
                "blocks_records": error,
                "entity": group["entity"],
                "error_type": group["error_type"],
                "records": group["records"],
                "title": action.title.format(**params),
                "question": _question(action, group, params),
                "fields": group["fields"],
                "values": group["values"],
                "rule": group["rule"],
            }
        )
    kind_order = {"decision": 0, "data_fix": 1, "dependent": 2, "review": 3}
    items.sort(key=lambda i: (not i["blocks_records"], kind_order[i["kind"]], -i["records"]))
    return items


def _question(action: Action, group: dict[str, Any], params: dict[str, str]) -> str | None:
    rule = (group["rule"] or "").lower()
    for phrase, text in RULE_QUESTIONS.items():
        if phrase in rule:
            return text.format(**params)
    return action.question.format(**params) if action.question else None


def customer_questions(facts: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [
        {
            "source": "mapping review",
            "code": f"question:{q['id'][:8]}",
            "question": q["question"],
            "records": None,
        }
        for q in facts["open_questions"]
    ]
    out += [
        {
            "source": "validation",
            "code": i["code"],
            "question": i["question"],
            "records": i["records"],
        }
        for i in items
        if i["kind"] == "decision" and i["question"]
    ]
    return out


def next_steps(
    facts: dict[str, Any],
    status: ReadinessStatus,
    gates: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> list[str]:
    blocked = {g["code"] for g in gates if g["outcome"] == BLOCKER}
    steps: list[str] = []
    m = facts["mappings"]
    if "mappings_decided" in blocked:
        steps.append(
            f"Finish the mapping review: decide the {_fmt(m['undecided'])} open fields and get "
            f"answers to the {_fmt(m['open_questions'])} open customer questions."
        )
    run = facts["dry_run"]
    if run is None or "rehearsal" in blocked or "rehearsal_current" in blocked:
        steps.append("Run a full dry run (no record limit) against the current plan.")
    if "reconciliation" in blocked and run is not None and facts["reconciliation"] is not None:
        steps.append(
            "Explain every reconciliation discrepancy before anything else; until the numbers "
            "add up, no other number in this report can be trusted."
        )
    if "target_accepted" in blocked:
        steps.append(
            "Read every refusal on the Dry run page, add the missing validation check or fix the "
            "target fault, and rehearse again."
        )
    decisions = [i for i in items if i["kind"] == "decision"]
    fixes = [i for i in items if i["kind"] == "data_fix"]
    dependent = sum(i["records"] for i in items if i["kind"] == "dependent")
    reviews = [i for i in items if i["kind"] == "review"]
    open_q = len(facts["open_questions"])
    if decisions or open_q:
        steps.append(
            f"Send the customer the {_fmt(len(decisions) + open_q)} questions in this report and "
            "record each answer as a rule in the transformation configuration or as a mapping "
            "decision."
        )
    if fixes:
        fix_records = sum(i["records"] for i in fixes)
        steps.append(
            f"Send the customer the record lists for {_fmt(len(fixes))} data corrections "
            f"({_fmt(fix_records)} records). Each list comes from the run's issues, filtered by "
            "problem, with the source row of every record."
        )
    if dependent:
        steps.append(
            f"Expect up to {_fmt(dependent)} more records to migrate once their accounts are "
            "fixed: they are excluded only because the account they belong to is."
        )
    if decisions or fixes:
        steps.append(
            "When the corrected export arrives: re-profile, re-run the dry run, and generate "
            "this report again."
        )
    if status is not ReadinessStatus.blocked:
        if reviews or any(g["outcome"] == CONDITION for g in gates):
            steps.append(
                "Get the customer's written sign-off on the records that will not migrate and "
                "on the warnings listed under conditions."
            )
        steps.append("Schedule the production migration window with the customer.")
    if run is not None and run["namespace_kept"]:
        steps.append(
            f"Purge staging namespace {run['namespace']} once the customer has signed off; it "
            "holds a copy of their data."
        )
    return steps


def technical_risks(
    facts: dict[str, Any], settings: Settings | None = None
) -> list[dict[str, str]]:
    settings = settings or get_settings()
    risks: list[dict[str, str]] = []
    run = facts["dry_run"]
    m = facts["mappings"]
    if run is not None:
        totals = run["totals"]
        attempted = int(totals.get("attempted", 0))
        if attempted:
            per_record_ms = run["duration_seconds"] * 1000 / attempted
            ten_x = attempted * 10 * per_record_ms / 60000
            risks.append(
                {
                    "code": "sequential_writes",
                    "title": "Writes are sequential",
                    "detail": f"the rehearsal sent {_fmt(attempted)} records in "
                    f"{run['duration_seconds']} seconds, about {per_record_ms:.1f} ms each. At "
                    f"ten times the volume that is roughly {ten_x:.0f} minutes (a projection "
                    "from this run, not a measurement). Plan the production window for it or "
                    "batch the writes.",
                }
            )
        rules = run["applied_rules"]
        dated = [r for r in rules if "dormant" in r or "trial" in r]
        if dated and run["as_of"]:
            risks.append(
                {
                    "code": "pinned_date",
                    "title": "Date-based rules are measured from a fixed date",
                    "detail": f"dormancy and trial rules are measured from {run['as_of']} (the "
                    "configuration's as_of). If go-live is later, more accounts become dormant "
                    "and more trials expire; move as_of to the go-live date and rehearse again.",
                }
            )
        warned = int(totals.get("with_warnings", 0))
        if warned:
            risks.append(
                {
                    "code": "warnings_migrate",
                    "title": "Records with warnings will migrate",
                    "detail": f"{_fmt(warned)} records carry a warning and are not held back. "
                    "Each one is a known imperfection the customer is accepting.",
                }
            )
        if run["issues_truncated"] or run["failures_truncated"]:
            risks.append(
                {
                    "code": "truncated_detail",
                    "title": "Not every issue was stored",
                    "detail": "the run found more issues or refusals than it keeps in full. The "
                    "counts are complete; the record lists are not. Raise "
                    "VALIDATION_ISSUE_LIMIT or MIGRATION_FAILURE_LIMIT for the next run.",
                }
            )
        retries = sum(int(s.get("retries", 0)) for s in run["stats"].values())
        if retries:
            risks.append(
                {
                    "code": "target_retries",
                    "title": "The target needed retries",
                    "detail": f"{_fmt(retries)} writes were retried. Retries are safe by the id "
                    "contract, but a flaky target in production lengthens the window.",
                }
            )
        if run["namespace_kept"]:
            risks.append(
                {
                    "code": "staging_copy",
                    "title": "A copy of the customer's data sits in staging",
                    "detail": f"namespace {run['namespace']} holds every accepted record. Purge "
                    "it after sign-off.",
                }
            )
    if m["approved_below_high_confidence"]:
        risks.append(
            {
                "code": "low_confidence_mappings",
                "title": "Some approved mappings had a low model confidence",
                "detail": f"{_fmt(m['approved_below_high_confidence'])} approved mappings came "
                f"in below the {m['high_confidence_threshold']} confidence threshold. A person "
                "approved each one; worth a second look before production.",
            }
        )
    if facts["environment"]["target_fault_rate"]:
        risks.append(
            {
                "code": "fault_injection",
                "title": "Fault injection is switched on in this environment",
                "detail": f"TARGET_FAULT_RATE is {facts['environment']['target_fault_rate']}. "
                "Refusals in this run may be injected rather than real.",
            }
        )
    return [{**risk, "detail": _cap(risk["detail"])} for risk in risks]


# --- the summary ---------------------------------------------------------------------------------


def _coverage_line(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for entity, s in run["stats"].items():
        in_scope = int(s.get("built", 0)) - int(s.get("skipped_by_rule", 0))
        accepted = int(s.get("accepted", 0))
        out[entity] = {
            "in_scope": _fmt(in_scope),
            "accepted": _fmt(accepted),
            "stay_behind": _fmt(in_scope - accepted),
            "landed_percent": _pct(accepted, in_scope),
        }
    return out


def summary_facts(content: dict[str, Any]) -> dict[str, Any]:
    """the facts the model is shown: formatted the way they may be quoted."""
    facts = content["facts"]
    run = facts["dry_run"]
    rec = facts["reconciliation"]
    out: dict[str, Any] = {
        "status": content["status"],
        "customer": facts["project"]["customer"],
        "project": facts["project"]["name"],
        "target": f"{PLATFORM_NAME} {facts['project']['target_environment']}",
        "gates": [
            {"outcome": g["outcome"], "title": g["title"], "detail": g["detail"]}
            for g in content["gates"]
            if g["outcome"] != PASS
        ],
        "work_items": [
            {
                "code": i["code"],
                "kind": i["kind"],
                "owner": i["owner"],
                "records": _fmt(i["records"]),
                "title": i["title"],
            }
            for i in content["work_items"]
        ],
        "customer_questions": _fmt(len(content["customer_questions"])),
        "work_counts": {
            kind: _fmt(sum(1 for i in content["work_items"] if i["kind"] == kind))
            for kind in ("decision", "data_fix", "dependent", "review")
        },
        "mapping": {
            "approved": _fmt(facts["mappings"]["approved"]),
            "undecided": _fmt(facts["mappings"]["undecided"]),
            "open_questions": _fmt(facts["mappings"]["open_questions"]),
        },
    }
    if run is not None:
        t = run["totals"]
        in_scope = int(t.get("built", 0)) - int(t.get("skipped_by_rule", 0))
        out["rehearsal"] = {
            "date": (run["started_at"] or "")[:10],
            "source_rows": _fmt(int(t.get("source_rows", 0))),
            "skipped_by_customer_rules": _fmt(int(t.get("skipped_by_rule", 0))),
            "in_scope": _fmt(in_scope),
            "valid": _fmt(int(t.get("valid", 0))),
            "cannot_migrate_as_they_stand": _fmt(int(t.get("invalid", 0))),
            "sent": _fmt(int(t.get("attempted", 0))),
            "accepted": _fmt(int(t.get("accepted", 0))),
            "refused_or_failed": _fmt(int(t.get("rejected", 0)) + int(t.get("failed", 0))),
            "landed_percent": _pct(int(t.get("accepted", 0)), in_scope),
            "with_warnings": _fmt(int(t.get("with_warnings", 0))),
            "by_entity": _coverage_line(run),
        }
    if rec is not None:
        out["reconciliation"] = {
            "status": rec["status"],
            "checks_passed": _fmt(rec["checks_passed"]),
            "checks_total": _fmt(rec["checks_total"]),
            "discrepancies": _fmt(len(rec["discrepancies"])),
        }
    return out


def _explain_codes(items: list[dict[str, Any]], limit: int = 5) -> list[str]:
    """the largest groups of records held back by something the customer can act on."""
    actionable = [i for i in items if i["blocks_records"] and i["kind"] in ("decision", "data_fix")]
    actionable.sort(key=lambda i: -i["records"])
    return [i["code"] for i in actionable[:limit]]


def _numbers(text: str) -> set[str]:
    out = set()
    for match in _NUMBER.findall(text):
        value = match.replace(",", "").rstrip(".")
        if value:
            out.add(value)
    return out


def check_summary(
    summary: ReadinessSummary, *, facts: dict[str, Any], status: str, codes: list[str]
) -> list[str]:
    """what the schema cannot enforce: the status as decided, only numbers from the facts, only
    real work items. every problem is phrased so it can go back to the model verbatim."""
    problems: list[str] = []
    allowed = _numbers(json.dumps(facts, ensure_ascii=False))
    texts = [summary.headline, summary.summary] + [
        b.explanation for b in summary.blocker_explanations
    ]
    whole = "\n".join(texts)
    invented = sorted(_numbers(whole) - allowed, key=lambda v: (len(v), v))
    if invented:
        problems.append(
            "these numbers do not appear in the facts: "
            + ", ".join(invented[:12])
            + ". Quote numbers exactly as the facts write them, or describe them in words"
        )
    if status not in summary.headline:
        problems.append(f"the headline must state the status exactly as given: {status}")
    remainder = whole.replace(status, "")
    for word in _STATUS_WORDS:
        if re.search(rf"\b{word}\b", remainder):
            problems.append(
                f"'{word}' is not this report's status; the status is {status} and no other "
                "status word may appear"
            )
            break
    given = [b.code for b in summary.blocker_explanations]
    missing = [c for c in codes if c not in given]
    unknown = [c for c in given if c not in codes]
    if missing:
        problems.append(f"missing blocker explanations for: {', '.join(missing)}")
    if unknown:
        problems.append(
            f"blocker explanations for codes that were not asked for: {', '.join(unknown)}"
        )
    if not summary.summary.strip():
        problems.append("the summary is empty")
    return problems


def _no_em_dash(text: str) -> str:
    return text.replace(" — ", ", ").replace("—", ", ")


def template_summary(content: dict[str, Any]) -> dict[str, Any]:
    """the summary written from the facts by code. used with no model, or when the model's
    draft did not pass the check."""
    facts = content["facts"]
    status = content["status"]
    project = facts["project"]
    run = facts["dry_run"]
    rec = facts["reconciliation"]
    headline = (
        f"{project['customer']} is {status} for go-live into {PLATFORM_NAME} "
        f"{project['target_environment']}."
    )
    parts: list[str] = []
    if run is None:
        parts.append(
            "The migration has not been rehearsed yet, so there is nothing to judge readiness on."
        )
    else:
        t = run["totals"]
        in_scope = int(t.get("built", 0)) - int(t.get("skipped_by_rule", 0))
        parts.append(
            f"The rehearsal on {(run['started_at'] or '')[:10]} followed "
            f"{_fmt(int(t.get('source_rows', 0)))} source rows. The customer's own rules set "
            f"aside {_fmt(int(t.get('skipped_by_rule', 0)))}, leaving {_fmt(in_scope)} records "
            f"in scope. {_fmt(int(t.get('valid', 0)))} passed validation and the target accepted "
            f"{_fmt(int(t.get('accepted', 0)))} of the {_fmt(int(t.get('attempted', 0)))} sent "
            f"({_pct(int(t.get('accepted', 0)), in_scope)} of the records in scope). "
            f"{_fmt(int(t.get('invalid', 0)))} records cannot migrate as they stand."
        )
        if rec is not None and rec["status"] == ReconciliationStatus.balanced.value:
            parts.append(
                "Every count reconciles, and the target holds exactly the records the rehearsal "
                "accepted."
            )
        elif rec is not None:
            parts.append(
                f"Reconciliation found {_fmt(len(rec['discrepancies']))} discrepancies that have "
                "to be explained before go-live."
            )
    blockers = [g for g in content["gates"] if g["outcome"] == BLOCKER]
    conditions = [g for g in content["gates"] if g["outcome"] == CONDITION]
    if blockers:
        parts.append(
            "Go-live is blocked by: " + "; ".join(g["title"].lower() for g in blockers) + "."
        )
    elif conditions:
        parts.append(
            "Go-live can proceed on conditions: "
            + "; ".join(g["title"].lower() for g in conditions)
            + "."
        )
    decisions = sum(1 for i in content["work_items"] if i["kind"] == "decision")
    fixes = sum(1 for i in content["work_items"] if i["kind"] == "data_fix")
    if decisions or fixes:
        parts.append(
            f"Closing the gap takes {_fmt(decisions)} decisions from the customer and "
            f"{_fmt(fixes)} sets of data corrections, listed below with their record counts."
        )
    by_code = {i["code"]: i for i in content["work_items"]}
    explanations = []
    for code in _explain_codes(content["work_items"]):
        item = by_code[code]
        why = {
            "decision": "Only the customer can decide this; the tool will not guess.",
            "data_fix": "The records stay behind until the customer corrects them in the export.",
        }.get(item["kind"], "")
        explanations.append({"code": code, "explanation": f"{item['title']}. {why}".strip()})
    return {
        "headline": headline,
        "summary": " ".join(parts),
        "blocker_explanations": explanations,
    }


@dataclass
class SummaryOutcome:
    summary: dict[str, Any]
    origin: str
    note: str | None = None
    call: LlmCall | None = None


def _find_cached(session: Session, project_id: uuid.UUID, input_hash: str) -> LlmCall | None:
    return session.execute(
        select(LlmCall)
        .where(
            LlmCall.project_id == project_id,
            LlmCall.input_hash == input_hash,
            LlmCall.purpose == PURPOSE_SUMMARY,
            LlmCall.status == "ok",
            LlmCall.cached.is_(False),
            LlmCall.response.is_not(None),
        )
        .order_by(LlmCall.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def draft_summary(
    session: Session,
    project: OnboardingProject,
    content: dict[str, Any],
    *,
    provider: LlmProvider,
    use_model: bool,
    settings: Settings | None = None,
) -> SummaryOutcome:
    """ask the model for the summary, check it against the facts, fall back to the template."""
    settings = settings or get_settings()
    if not use_model or provider.name == "none":
        note = (
            "model drafting was turned off for this report"
            if not use_model
            else "no model is configured, so the summary is written from the facts by code"
        )
        return SummaryOutcome(summary=template_summary(content), origin="template", note=note)

    facts = summary_facts(content)
    codes = _explain_codes(content["work_items"])
    bundle = build_summary_prompt(
        facts, explain=codes, provider=provider.name, model=provider.model
    )
    hit = _find_cached(session, project.id, bundle.input_hash)
    if hit is not None:
        session.add(
            LlmCall(
                project_id=project.id,
                purpose=PURPOSE_SUMMARY,
                provider=provider.name,
                model=hit.model,
                prompt_version=REPORT_PROMPT_VERSION,
                input_hash=bundle.input_hash,
                cached=True,
                status="ok",
                attempts=0,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0.0,
            )
        )
        log.info("readiness_summary_cached", llm_call_id=str(hit.id))
        return SummaryOutcome(summary=dict(hit.response or {}), origin="model", call=hit)

    call = LlmCall(
        project_id=project.id,
        purpose=PURPOSE_SUMMARY,
        provider=provider.name,
        model=provider.model,
        prompt_version=REPORT_PROMPT_VERSION,
        input_hash=bundle.input_hash,
        cached=False,
        status="error",
        attempts=0,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0.0,
    )
    session.add(call)
    feedback = ""
    problems: list[str] = []
    while call.attempts < settings.llm_max_attempts:
        call.attempts += 1
        try:
            result: StructuredResult[ReadinessSummary] = provider.complete(
                system=bundle.system,
                user=bundle.user + feedback,
                output=ReadinessSummary,
                max_tokens=settings.llm_max_output_tokens,
            )
        except (LlmError, LlmUnavailableError) as exc:
            call.error = exc.message
            log.warning("readiness_summary_failed", error=exc.message, attempt=call.attempts)
            return SummaryOutcome(
                summary=template_summary(content),
                origin="template",
                note=f"the model could not be reached ({exc.message}); the summary is written "
                "from the facts by code",
                call=call,
            )
        call.model = result.model
        call.input_tokens = (call.input_tokens or 0) + (result.input_tokens or 0)
        call.output_tokens = (call.output_tokens or 0) + (result.output_tokens or 0)
        call.latency_ms = round((call.latency_ms or 0) + result.latency_ms, 1)
        call.request_id = result.request_id
        parsed = result.parsed
        parsed = ReadinessSummary(
            headline=_no_em_dash(parsed.headline),
            summary=_no_em_dash(parsed.summary),
            blocker_explanations=[
                b.model_copy(update={"explanation": _no_em_dash(b.explanation)})
                for b in parsed.blocker_explanations
            ],
        )
        problems = check_summary(parsed, facts=facts, status=content["status"], codes=codes)
        if not problems:
            call.status = "ok"
            call.response = parsed.model_dump()
            log.info(
                "readiness_summary",
                model=call.model,
                attempts=call.attempts,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                duration_ms=call.latency_ms,
            )
            return SummaryOutcome(summary=parsed.model_dump(), origin="model", call=call)
        log.warning("readiness_summary_rejected", attempt=call.attempts, problems=problems[:5])
        feedback = (
            "\n\nYour previous answer had these problems. Fix them and answer again in full:\n- "
            + "\n- ".join(problems)
        )
    call.error = "; ".join(problems)[:2000]
    return SummaryOutcome(
        summary=template_summary(content),
        origin="template",
        note=f"the model's draft failed the check against the facts {call.attempts} times "
        f"({problems[0]}); the summary is written from the facts by code",
        call=call,
    )


# --- the report ----------------------------------------------------------------------------------


def build_content(
    session: Session, project: OnboardingProject, settings: Settings | None = None
) -> dict[str, Any]:
    """the whole report except the summary, from stored facts and stated rules."""
    settings = settings or get_settings()
    facts = collect_facts(session, project, settings)
    status, gates = assess(facts)
    items = work_items(facts)
    return {
        "report_version": REPORT_VERSION,
        "status": status.value,
        "facts": facts,
        "gates": gates,
        "blockers": [g for g in gates if g["outcome"] == BLOCKER],
        "conditions": [g for g in gates if g["outcome"] == CONDITION],
        "work_items": items,
        "customer_questions": customer_questions(facts, items),
        "next_steps": next_steps(facts, status, gates, items),
        "technical_risks": technical_risks(facts, settings),
    }


def generate_report(
    session: Session,
    project: OnboardingProject,
    *,
    provider: LlmProvider,
    use_model: bool = True,
    generated_by: str | None = None,
    settings: Settings | None = None,
) -> ReadinessReport:
    with stage("readiness", project_id=str(project.id)) as log_report:
        report = _generate_report(
            session,
            project,
            provider=provider,
            use_model=use_model,
            generated_by=generated_by,
            settings=settings,
        )
        log_report.note(status=report.status.value, summary_origin=report.summary_origin)
        return report


def _generate_report(
    session: Session,
    project: OnboardingProject,
    *,
    provider: LlmProvider,
    use_model: bool,
    generated_by: str | None,
    settings: Settings | None,
) -> ReadinessReport:
    settings = settings or get_settings()
    started = time.perf_counter()
    content = build_content(session, project, settings)
    outcome = draft_summary(
        session, project, content, provider=provider, use_model=use_model, settings=settings
    )
    content["summary"] = {
        **outcome.summary,
        "origin": outcome.origin,
        "model": outcome.call.model if outcome.call and outcome.origin == "model" else None,
        "note": outcome.note,
    }
    session.flush()
    facts = content["facts"]
    report = ReadinessReport(
        id=uuid.uuid4(),
        project_id=project.id,
        run_id=uuid.UUID(facts["dry_run"]["id"]) if facts["dry_run"] else None,
        reconciliation_id=uuid.UUID(facts["reconciliation"]["id"])
        if facts["reconciliation"]
        else None,
        status=ReadinessStatus(content["status"]),
        content=content,
        markdown="",
        summary_origin=outcome.origin,
        llm_call_id=outcome.call.id if outcome.call else None,
        generated_by=generated_by,
    )
    content["report_id"] = str(report.id)
    report.markdown = render_markdown(content)
    report.content = content
    session.add(report)
    workflow.complete(project, Event.reported)
    session.commit()
    log.info(
        "readiness_report",
        report_id=str(report.id),
        status=report.status.value,
        summary_origin=outcome.origin,
        blockers=len(content["blockers"]),
        conditions=len(content["conditions"]),
        work_items=len(content["work_items"]),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return report


def preview(
    session: Session, project: OnboardingProject, settings: Settings | None = None
) -> dict[str, Any]:
    """the report as it would be generated now, with the template summary, stored nowhere."""
    content = build_content(session, project, settings)
    content["summary"] = {
        **template_summary(content),
        "origin": "template",
        "model": None,
        "note": "a preview: nothing is stored until a report is generated",
    }
    content["report_id"] = None
    return content


def latest_report(session: Session, project: OnboardingProject) -> ReadinessReport | None:
    return session.execute(
        select(ReadinessReport)
        .where(ReadinessReport.project_id == project.id)
        .order_by(ReadinessReport.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def list_reports(
    session: Session, project: OnboardingProject, limit: int = 50
) -> list[ReadinessReport]:
    return list(
        session.execute(
            select(ReadinessReport)
            .where(ReadinessReport.project_id == project.id)
            .order_by(ReadinessReport.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def get_report(
    session: Session, project: OnboardingProject, report_id: uuid.UUID
) -> ReadinessReport:
    found = session.execute(
        select(ReadinessReport).where(
            ReadinessReport.id == report_id, ReadinessReport.project_id == project.id
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFoundError(
            f"no readiness report {report_id} on this project",
            details={"report_id": str(report_id)},
        )
    return found


# --- markdown ------------------------------------------------------------------------------------


def _md_table(headers: list[str], rows: list[list[Any]], align: list[str] | None = None) -> str:
    align = align or ["l"] * len(headers)
    rule = ["---:" if a == "r" else "---" for a in align]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(rule) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(lines)


def render_markdown(content: dict[str, Any]) -> str:
    """the report as markdown: an executive part for the customer, then the technical appendix."""
    facts = content["facts"]
    project = facts["project"]
    run = facts["dry_run"]
    rec = facts["reconciliation"]
    summary = content["summary"]
    out: list[str] = []
    w = out.append

    w(f"# Implementation readiness: {project['customer']}")
    w("")
    w(f"**Status: {content['status']}**")
    w("")
    w(
        f"Project: {project['name']} · Target: {PLATFORM_NAME} {project['target_environment']} · "
        f"Generated {_when(facts['generated_at'])}"
        + (f" · Report {content['report_id'][:8]}" if content.get("report_id") else "")
    )
    w("")
    w("## Executive summary")
    w("")
    w(f"**{summary['headline']}**")
    w("")
    w(summary["summary"])
    w("")
    if summary.get("blocker_explanations"):
        w("### What stands in the way")
        w("")
        for b in summary["blocker_explanations"]:
            w(f"- **{b['code']}**: {b['explanation']}")
        w("")
    origin = (
        f"Drafted by {summary['model']} from the facts below and checked against them."
        if summary["origin"] == "model"
        else "Written from the facts below by the tool."
    )
    if summary.get("note"):
        origin += f" Note: {summary['note']}."
    w(f"_{origin}_")
    w("")

    w("## Blockers")
    w("")
    if content["blockers"]:
        for g in content["blockers"]:
            w(f"- **{g['title']}.** {g['detail']}")
    else:
        w("None.")
    w("")
    w("## Conditions")
    w("")
    if content["conditions"]:
        for g in content["conditions"]:
            w(f"- **{g['title']}.** {g['detail']}")
    else:
        w("None.")
    w("")

    items = content["work_items"]
    if items:
        w("## Work before go-live")
        w("")
        w(
            _md_table(
                ["work", "owner", "records", "blocks records"],
                [
                    [
                        i["title"],
                        i["owner"],
                        _fmt(i["records"]),
                        "yes" if i["blocks_records"] else "no",
                    ]
                    for i in items
                ],
                ["l", "l", "r", "l"],
            )
        )
        w("")
        w(
            "A record with two problems appears in both rows, so the records column can add up "
            "to more than the records excluded. The reconciliation in the appendix counts each "
            "record once, by its first error."
        )
        w("")

    w("## Customer questions")
    w("")
    if content["customer_questions"]:
        for n, q in enumerate(content["customer_questions"], 1):
            w(f"{n}. {q['question']}")
    else:
        w("None.")
    w("")
    w("## Next steps")
    w("")
    for n, step in enumerate(content["next_steps"], 1):
        w(f"{n}. {step}")
    w("")
    w("## Technical risks")
    w("")
    if content["technical_risks"]:
        for r in content["technical_risks"]:
            w(f"- **{r['title']}.** {r['detail']}")
    else:
        w("None recorded.")
    w("")

    w("---")
    w("")
    w("# Technical appendix")
    w("")
    w("## Readiness gates")
    w("")
    w(
        _md_table(
            ["gate", "outcome", "detail"],
            [[g["title"], g["outcome"], g["detail"]] for g in content["gates"]],
        )
    )
    w("")
    w(
        f"Policy: an entity below {facts['policy']['min_entity_coverage'] * 100:.0f}% of its "
        "in-scope records landing is a blocker; any record left behind or any warning is a "
        "condition."
    )
    w("")

    w("## Data profile")
    w("")
    profile = facts["data_profile"]
    w(
        _md_table(
            ["dataset", "kind", "rows", "columns", "errors", "warnings", "info"],
            [
                [
                    d["name"],
                    d["kind"],
                    _fmt(d["rows"] or 0),
                    _fmt(d["columns"] or 0),
                    _fmt(d["errors"]),
                    _fmt(d["warnings"]),
                    _fmt(d["info"]),
                ]
                for d in profile["datasets"]
            ],
            ["l", "l", "r", "r", "r", "r", "r"],
        )
    )
    w("")
    w(f"{_fmt(profile['rows'])} rows across {_fmt(profile['profiled'])} profiled sources.")
    w("")

    w("## Mappings")
    w("")
    m = facts["mappings"]
    w(
        _md_table(
            ["approved", "not migrated", "rejected", "undecided", "open questions"],
            [
                [
                    _fmt(m["approved"]),
                    _fmt(m["ignored"]),
                    _fmt(m["rejected"]),
                    _fmt(m["undecided"]),
                    _fmt(m["open_questions"]),
                ]
            ],
            ["r"] * 5,
        )
    )
    w("")
    if m["approved_by_origin"]:
        origins = ", ".join(f"{k} {_fmt(v)}" for k, v in sorted(m["approved_by_origin"].items()))
        w(f"Approved mappings by origin: {origins}.")
        w("")
    if m["required_missing"]:
        w("Required target fields with no approved mapping: " + ", ".join(m["required_missing"]))
        w("")

    if run is not None:
        w("## Validation")
        w("")
        t = run["totals"]
        w(
            _md_table(
                ["source rows", "skipped by rule", "valid", "excluded", "with warnings"],
                [
                    [
                        _fmt(int(t.get("source_rows", 0))),
                        _fmt(int(t.get("skipped_by_rule", 0))),
                        _fmt(int(t.get("valid", 0))),
                        _fmt(int(t.get("invalid", 0))),
                        _fmt(int(t.get("with_warnings", 0))),
                    ]
                ],
                ["r"] * 5,
            )
        )
        w("")
        groups = facts["issue_groups"]
        if groups:
            w(
                _md_table(
                    ["severity", "entity", "problem", "records", "fields"],
                    [
                        [
                            g["severity"],
                            g["entity"],
                            g["error_type"],
                            _fmt(g["records"]),
                            ", ".join(g["fields"]),
                        ]
                        for g in groups
                    ],
                    ["l", "l", "l", "r", "l"],
                )
            )
            w("")
        if run["applied_rules"]:
            w("Customer rules applied:")
            w("")
            for rule, count in run["applied_rules"].items():
                w(f"- {rule}: {_fmt(int(count))} records")
            w("")

        w("## Migration dry run")
        w("")
        w(
            _md_table(
                [
                    "entity",
                    "valid",
                    "sent",
                    "accepted",
                    "refused",
                    "failed",
                    "blocked",
                    "not reached",
                    "retries",
                ],
                [
                    [
                        e,
                        _fmt(int(s.get("valid", 0))),
                        _fmt(int(s.get("attempted", 0))),
                        _fmt(int(s.get("accepted", 0))),
                        _fmt(int(s.get("rejected", 0))),
                        _fmt(int(s.get("failed", 0))),
                        _fmt(int(s.get("blocked", 0))),
                        _fmt(int(s.get("not_attempted", 0))),
                        _fmt(int(s.get("retries", 0))),
                    ]
                    for e, s in run["stats"].items()
                ],
                ["l"] + ["r"] * 8,
            )
        )
        w("")
        w(
            f"Dry run {run['id']} started {_when(run['started_at'])}, took "
            f"{run['duration_seconds']} seconds, configuration {run['config_version']}, dates "
            f"measured from {run['as_of']}."
        )
        w("")

    w("## Reconciliation")
    w("")
    if rec is None:
        w("No reconciliation recorded.")
        w("")
    else:
        w(
            f"**{rec['status']}**: {_fmt(rec['checks_passed'])} of {_fmt(rec['checks_total'])} "
            f"checks passed ({rec['trigger'].replace('_', ' ')}, {_when(rec['created_at'])})."
        )
        w("")
        w(
            _md_table(
                [
                    "entity",
                    "source rows",
                    "distinct ids",
                    "skipped by rule",
                    "in scope",
                    "excluded",
                    "valid",
                    "accepted",
                    "in target",
                    "landed",
                ],
                [
                    [
                        e,
                        _fmt(s["source_rows"]),
                        _fmt(s["distinct_ids"]),
                        _fmt(s["skipped_by_rule"]),
                        _fmt(s["in_scope"]),
                        _fmt(s["excluded"]),
                        _fmt(s["valid"]),
                        _fmt(s["accepted"]),
                        _fmt(s["in_target"]),
                        _pct(s["accepted"], s["in_scope"]),
                    ]
                    for e, s in rec["entities"].items()
                ],
                ["l"] + ["r"] * 9,
            )
        )
        w("")
        w("Why records were excluded (first error on each record):")
        w("")
        for e, s in rec["entities"].items():
            ordered = sorted(s["excluded_by_reason"].items(), key=lambda kv: (-kv[1], kv[0]))
            reasons = ", ".join(f"{k} {_fmt(v)}" for k, v in ordered)
            w(f"- {e}: {reasons or 'none'}")
        w("")
        if rec["failed_checks"]:
            w("Failed checks:")
            w("")
            for c in rec["failed_checks"]:
                w(
                    f"- {c['entity']} · {c['label']}: expected {_fmt(c['expected'])}, "
                    f"counted {_fmt(c['actual'])}. {c['detail']}"
                )
            w("")
        for d in rec["discrepancies"]:
            sample = ", ".join(d["sample_ids"][:5])
            w(
                f"- **{d['entity']} {d['kind']}** ({_fmt(d['count'])}): {d['explanation']}"
                + (f" Sample: {sample}." if sample else "")
            )
        if rec["discrepancies"]:
            w("")

    w("## Provenance")
    w("")
    w(f"- Project {project['id']}, stage {project['stage']}")
    if run is not None:
        w(f"- Dry run {run['id']}, staging namespace {run['namespace']}")
    if rec is not None:
        w(f"- Reconciliation {rec['id']}")
    w(
        f"- Summary: {summary['origin']}"
        + (f" ({summary['model']}, prompt {REPORT_PROMPT_VERSION})" if summary.get("model") else "")
    )
    w(
        "- Every number in this report is a count of stored rows or arithmetic over them. "
        "No number was produced by a model."
    )
    w("")
    return "\n".join(out)


__all__ = [
    "ACTIONS",
    "assess",
    "build_content",
    "check_summary",
    "collect_facts",
    "customer_questions",
    "draft_summary",
    "generate_report",
    "get_report",
    "latest_report",
    "list_reports",
    "next_steps",
    "preview",
    "render_markdown",
    "summary_facts",
    "technical_risks",
    "template_summary",
    "work_items",
]
