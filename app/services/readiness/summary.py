"""the summary: drafted by the configured model from the facts and checked by code, or written from
the facts by code when the model is off, unavailable or keeps failing the check.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.provider import LlmError, LlmProvider, LlmUnavailableError, StructuredResult
from app.ai.report_prompt import REPORT_PROMPT_VERSION, build_summary_prompt
from app.ai.schemas import ReadinessSummary
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.mapping import LlmCall
from app.models.project import OnboardingProject
from app.models.report import ReconciliationStatus
from app.services.readiness.vocabulary import (
    _NUMBER,
    _STATUS_WORDS,
    BLOCKER,
    CONDITION,
    PASS,
    PURPOSE_SUMMARY,
    _fmt,
    _pct,
)
from app.target.schema import PLATFORM_NAME

log = get_logger(__name__)


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
