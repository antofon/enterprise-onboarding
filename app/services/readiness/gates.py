"""gates: stated rules over the facts decide the status, and each gate says what it checked, what
it found and which threshold it used.
"""

from __future__ import annotations

import json
from typing import Any

from app.models.report import ReadinessStatus, ReconciliationStatus
from app.services.readiness.vocabulary import BLOCKER, CONDITION, PASS, PLURAL, _cap, _fmt, _pct


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
