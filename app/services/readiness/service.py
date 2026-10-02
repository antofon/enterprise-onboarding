"""the report as a whole: built from the four layers, stored once and never updated, read back."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.provider import LlmProvider
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger, stage
from app.models.project import OnboardingProject
from app.models.report import ReadinessReport, ReadinessStatus
from app.services import workflow
from app.services.readiness.facts import collect_facts
from app.services.readiness.gates import assess
from app.services.readiness.render import render_markdown
from app.services.readiness.summary import draft_summary, template_summary
from app.services.readiness.vocabulary import BLOCKER, CONDITION, REPORT_VERSION
from app.services.readiness.work import customer_questions, next_steps, technical_risks, work_items
from app.services.workflow import Event

log = get_logger(__name__)


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
