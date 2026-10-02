"""reconciliation: does what the dry run says it did add up, and does the target agree?

Two kinds of check, all arithmetic, none of it asked of a model:

  the funnel      every source row is followed through the run, per entity, and every stage has
                  to account for the one before it. rows become records one to one. a record is
                  skipped by a customer rule, excluded by validation, or valid, and nothing else.
                  every exclusion has a reason. a valid record was sent, blocked behind a refused
                  organization, or not reached (a partial run). a sent record was accepted,
                  refused or failed. a count that does not add up is a bug in this tool, and the
                  check names it.
  the target      the identifiers the run saw accepted are compared one by one with what the
                  target platform holds in the run's namespace, read back over its own api. a
                  record the run counted as accepted that is not there, or a record that is there
                  although the run counted it as failed (a write that landed and lost its
                  answer), is a discrepancy with sample identifiers.

Records that did not migrate for a stated reason are not discrepancies. They are the explained
gap, and the readiness report turns them into the customer's to-do list. A discrepancy is a
number nobody can explain, and it blocks go-live until somebody does.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import InvalidStateError, NotFoundError
from app.core.logging import get_logger
from app.models.migration import MigrationRun, RunKind, RunStatus
from app.models.project import OnboardingProject
from app.models.report import ReconciliationResult, ReconciliationStatus
from app.models.target import WRITE_ORDER

log = get_logger(__name__)

SAMPLE_IDS = 10
TRIGGER_RUN = "dry_run"
TRIGGER_RECHECK = "recheck"


@dataclass
class EntityLedger:
    """what one dry run knows about one entity when it finishes."""

    entity: str
    source_rows: int = 0
    distinct_ids: int = 0
    built: int = 0
    skipped_by_rule: int = 0
    excluded: int = 0
    excluded_by_reason: dict[str, int] = field(default_factory=dict)
    valid: int = 0
    not_attempted: int = 0
    blocked: int = 0
    attempted: int = 0
    accepted: int = 0
    rejected: int = 0
    failed: int = 0
    refusals_by_type: dict[str, int] = field(default_factory=dict)
    accepted_ids: list[str] = field(default_factory=list)
    refused_ids: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        return {
            "source_rows": self.source_rows,
            "distinct_ids": self.distinct_ids,
            "built": self.built,
            "skipped_by_rule": self.skipped_by_rule,
            "excluded": self.excluded,
            "excluded_by_reason": dict(self.excluded_by_reason),
            "valid": self.valid,
            "not_attempted": self.not_attempted,
            "blocked": self.blocked,
            "attempted": self.attempted,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "failed": self.failed,
            "refusals_by_type": dict(self.refusals_by_type),
        }

    @classmethod
    def from_counts(
        cls, entity: str, counts: dict[str, Any], accepted_ids: list[str]
    ) -> EntityLedger:
        """rebuild from a stored reconciliation, for a re-check."""
        known = {k: v for k, v in counts.items() if k in cls.__dataclass_fields__}
        known.pop("accepted_ids", None)
        known.pop("refused_ids", None)
        return cls(entity=entity, accepted_ids=list(accepted_ids), **known)


@dataclass
class Comparison:
    status: ReconciliationStatus
    entities: dict[str, dict[str, Any]]
    totals: dict[str, int]
    checks: list[dict[str, Any]]
    discrepancies: list[dict[str, Any]]


def _check(
    entity: str, name: str, label: str, expected: int, actual: int, detail: str
) -> dict[str, Any]:
    return {
        "entity": entity,
        "check": name,
        "label": label,
        "expected": expected,
        "actual": actual,
        "ok": expected == actual,
        "detail": detail,
    }


def compare(ledgers: list[EntityLedger], target_ids: dict[str, set[str]]) -> Comparison:
    """the whole reconciliation, as a pure function of what the run knew and what the target
    holds. no database, no http: the unit tests drive it directly."""
    checks: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []
    entities: dict[str, dict[str, Any]] = {}

    for ledger in ledgers:
        e = ledger.entity
        held = target_ids.get(e, set())
        accepted = set(ledger.accepted_ids)
        in_scope = ledger.built - ledger.skipped_by_rule
        checks.extend(
            [
                _check(
                    e,
                    "rows_to_records",
                    "every source row became one record",
                    ledger.source_rows,
                    ledger.built,
                    "source rows in the datasets that emit this entity against records built",
                ),
                _check(
                    e,
                    "records_accounted",
                    "every record was skipped, excluded or valid",
                    ledger.built,
                    ledger.skipped_by_rule + ledger.excluded + ledger.valid,
                    "built against skipped by a customer rule + excluded by validation + valid",
                ),
                _check(
                    e,
                    "exclusions_explained",
                    "every excluded record has a reason",
                    ledger.excluded,
                    sum(ledger.excluded_by_reason.values()),
                    "excluded against the sum of excluded records by their first error",
                ),
                _check(
                    e,
                    "valid_accounted",
                    "every valid record was sent, blocked or not reached",
                    ledger.valid,
                    ledger.attempted + ledger.blocked + ledger.not_attempted,
                    "valid against sent + blocked behind a refused organization + not reached",
                ),
                _check(
                    e,
                    "sent_accounted",
                    "every record sent was accepted, refused or failed",
                    ledger.attempted,
                    ledger.accepted + ledger.rejected + ledger.failed,
                    "sent against accepted + refused by the target + failed after retries",
                ),
                _check(
                    e,
                    "target_count",
                    "the target holds what the run accepted",
                    ledger.accepted,
                    len(held),
                    "accepted by the run against records in the namespace, read back from the "
                    "target api",
                ),
            ]
        )
        missing = sorted(accepted - held)
        unexpected = sorted(held - accepted)
        refused_but_present = sorted(set(ledger.refused_ids) & held)
        id_ok = not missing and not unexpected
        checks.append(
            {
                "entity": e,
                "check": "target_ids",
                "label": "the target holds exactly the accepted identifiers",
                "expected": len(accepted),
                "actual": len(accepted & held),
                "ok": id_ok,
                "detail": f"{len(missing)} accepted but not in the target, "
                f"{len(unexpected)} in the target but not accepted",
            }
        )
        if missing:
            discrepancies.append(
                {
                    "entity": e,
                    "kind": "missing_in_target",
                    "count": len(missing),
                    "sample_ids": missing[:SAMPLE_IDS],
                    "explanation": "the run counted these as accepted and the target does not "
                    "hold them. Either the namespace changed after the run or an "
                    "acceptance was misread.",
                }
            )
        if unexpected:
            landed = len(refused_but_present)
            discrepancies.append(
                {
                    "entity": e,
                    "kind": "unexpected_in_target",
                    "count": len(unexpected),
                    "sample_ids": unexpected[:SAMPLE_IDS],
                    "explanation": (
                        f"the target holds records the run did not count as accepted. {landed} "
                        "of them the run recorded as failed: the write landed and the answer was "
                        "lost or unreadable. A retry would have been a no-op by the id contract; "
                        "this run gave up first."
                        if landed
                        else "the target holds records the run did not count as accepted. "
                        "Something else wrote into this namespace."
                    ),
                }
            )
        entities[e] = {
            **ledger.counts(),
            "in_scope": in_scope,
            "in_target": len(held),
            "coverage": round(ledger.accepted / in_scope, 4) if in_scope else None,
        }

    for check in checks:
        if not check["ok"] and check["check"] not in ("target_count", "target_ids"):
            discrepancies.append(
                {
                    "entity": check["entity"],
                    "kind": check["check"],
                    "count": abs(check["expected"] - check["actual"]),
                    "sample_ids": [],
                    "explanation": f"{check['label']}: expected {check['expected']:,}, "
                    f"counted {check['actual']:,}. A count that does not add up is a defect "
                    "in the run itself.",
                }
            )

    keys = (
        "source_rows",
        "distinct_ids",
        "built",
        "skipped_by_rule",
        "in_scope",
        "excluded",
        "valid",
        "not_attempted",
        "blocked",
        "attempted",
        "accepted",
        "rejected",
        "failed",
        "in_target",
    )
    totals = {k: sum(int(v.get(k) or 0) for v in entities.values()) for k in keys}
    status = (
        ReconciliationStatus.balanced
        if all(c["ok"] for c in checks)
        else ReconciliationStatus.discrepancies
    )
    return Comparison(
        status=status,
        entities=entities,
        totals=totals,
        checks=checks,
        discrepancies=discrepancies,
    )


def _store(
    session: Session,
    run: MigrationRun,
    *,
    trigger: str,
    comparison: Comparison,
    ledgers: list[EntityLedger],
    target_counts: dict[str, int],
) -> ReconciliationResult:
    result = ReconciliationResult(
        id=uuid.uuid4(),
        project_id=run.project_id,
        run_id=run.id,
        trigger=trigger,
        status=comparison.status,
        namespace=run.namespace,
        entities=comparison.entities,
        totals=comparison.totals,
        checks=comparison.checks,
        discrepancies=comparison.discrepancies,
        ledger={ledger.entity: sorted(ledger.accepted_ids) for ledger in ledgers},
        target_counts=target_counts,
    )
    session.add(result)
    log.info(
        "reconciliation_complete",
        run_id=str(run.id),
        trigger=trigger,
        status=comparison.status.value,
        failed_checks=sum(1 for c in comparison.checks if not c["ok"]),
        accepted=comparison.totals["accepted"],
        in_target=comparison.totals["in_target"],
    )
    return result


def reconcile_run(
    session: Session,
    run: MigrationRun,
    *,
    ledgers: list[EntityLedger],
    target_ids: dict[str, set[str]],
    trigger: str = TRIGGER_RUN,
) -> ReconciliationResult:
    """called by the dry run as it finishes, with what it knows and what the target holds."""
    comparison = compare(ledgers, target_ids)
    counts = {entity: len(ids) for entity, ids in target_ids.items()}
    return _store(
        session,
        run,
        trigger=trigger,
        comparison=comparison,
        ledgers=ledgers,
        target_counts=counts,
    )


def latest_for_run(session: Session, run: MigrationRun) -> ReconciliationResult | None:
    return session.execute(
        select(ReconciliationResult)
        .where(ReconciliationResult.run_id == run.id)
        .order_by(ReconciliationResult.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def list_for_run(session: Session, run: MigrationRun) -> list[ReconciliationResult]:
    return list(
        session.execute(
            select(ReconciliationResult)
            .where(ReconciliationResult.run_id == run.id)
            .order_by(ReconciliationResult.created_at.desc())
        ).scalars()
    )


def get_reconciliation(
    session: Session, project: OnboardingProject, reconciliation_id: uuid.UUID
) -> ReconciliationResult:
    found = session.execute(
        select(ReconciliationResult).where(
            ReconciliationResult.id == reconciliation_id,
            ReconciliationResult.project_id == project.id,
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFoundError(
            f"no reconciliation {reconciliation_id} on this project",
            details={"reconciliation_id": str(reconciliation_id)},
        )
    return found


def _first(session: Session, run: MigrationRun) -> ReconciliationResult | None:
    """the reconciliation the run made itself, which carries what the run saw."""
    return session.execute(
        select(ReconciliationResult)
        .where(
            ReconciliationResult.run_id == run.id,
            ReconciliationResult.trigger == TRIGGER_RUN,
        )
        .order_by(ReconciliationResult.created_at)
        .limit(1)
    ).scalar_one_or_none()


def recheck(session: Session, run: MigrationRun, *, target: Any) -> ReconciliationResult:
    """read the target again and compare it with what the run saw. catches a namespace that was
    changed after the run: purged, written to, or partly deleted.

    `target` is the migration layer's TargetClient for the run's namespace."""
    if run.kind is not RunKind.dry_run:
        raise InvalidStateError(
            "only a dry run can be reconciled; a validation pass sends nothing to the target",
            details={"run_id": str(run.id), "kind": run.kind.value},
        )
    if run.status is not RunStatus.completed:
        raise InvalidStateError(
            f"this run {run.status.value}; there is nothing to reconcile",
            details={"run_id": str(run.id)},
        )
    if (run.options or {}).get("purge_namespace_after"):
        raise InvalidStateError(
            "the staging namespace was purged when this run finished; its reconciliation was "
            "made before the purge and there is nothing left to read",
            details={"run_id": str(run.id)},
        )
    original = _first(session, run)
    if original is None:
        raise InvalidStateError(
            "this run was made before reconciliation was recorded with the run; rehearse again "
            "to get a run that can be re-checked",
            details={"run_id": str(run.id)},
        )
    ledgers = [
        EntityLedger.from_counts(entity, original.entities[entity], original.ledger.get(entity, []))
        for entity in WRITE_ORDER
        if entity in original.entities
    ]
    target_ids = {ledger.entity: target.list_ids(ledger.entity) for ledger in ledgers}
    result = reconcile_run(
        session, run, ledgers=ledgers, target_ids=target_ids, trigger=TRIGGER_RECHECK
    )
    session.commit()
    return result


def first_errors(drafts: list[Any]) -> dict[str, int]:
    """the reason each excluded record is excluded: its first error, in the order the pipeline
    found them (conversion, then the contract, then the batch checks)."""
    counter: Counter[str] = Counter()
    for draft in drafts:
        if draft.skipped:
            continue
        errors = draft.errors
        if errors:
            counter[errors[0].error_type] += 1
    return dict(counter.most_common())


def read_target_ids(target: Any, entities: list[str]) -> dict[str, set[str]]:
    """every identifier in the namespace, per entity. an unreachable target is an empty answer
    and the checks then say so, rather than the run failing after every write landed."""
    out: dict[str, set[str]] = {}
    for entity in entities:
        try:
            out[entity] = target.list_ids(entity)
        except httpx.HTTPError as exc:
            log.warning("target_ids_unavailable", entity=entity, error=str(exc)[:200])
            out[entity] = set()
    return out


__all__ = [
    "Comparison",
    "EntityLedger",
    "compare",
    "first_errors",
    "get_reconciliation",
    "latest_for_run",
    "list_for_run",
    "read_target_ids",
    "recheck",
    "reconcile_run",
]
