"""work: the issues grouped into what somebody has to do, and the customer questions, next steps
and technical risks that follow from them by rule.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings, get_settings
from app.models.report import ReadinessStatus
from app.services.readiness.vocabulary import (
    ACTIONS,
    BLOCKER,
    CONDITION,
    FALLBACK_ERROR,
    FALLBACK_WARNING,
    OWNERS,
    PLURAL,
    RULE_QUESTIONS,
    Action,
    _cap,
    _fmt,
    _plain_values,
    _quote_values,
)
from app.target.schema import PLATFORM_NAME


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
