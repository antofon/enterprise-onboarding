"""propose where every source field lands; keep a person in charge of the decision.

two ways to get proposals: the model (structured output, checked against the catalog, retried
with feedback when it breaks the contract, cached by input hash) or, in manual mode, the
deterministic comparison itself (origin `heuristic`, no confidence invented). either way the
result is a row per field with a status, and only a person moves a row to approved, rejected
or ignored."""

from __future__ import annotations

import contextvars
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.ai.prompts import (
    PROMPT_VERSION,
    PromptBundle,
    build_mapping_prompt,
    clarification_template,
)
from app.ai.provider import LlmError, LlmProvider, StructuredResult
from app.ai.schemas import MappingProposal, SuggestedMapping
from app.core.config import Settings, get_settings
from app.core.errors import AppError, InvalidStateError, NotFoundError
from app.core.logging import get_logger
from app.models.mapping import (
    ClarificationQuestion,
    FieldMapping,
    LlmCall,
    MappingOrigin,
    MappingStatus,
    QuestionStatus,
)
from app.models.project import OnboardingProject, ProjectStage, SourceDataset
from app.schemas.mapping import (
    BulkApproveResult,
    DatasetSuggestResult,
    EntityMappingCoverage,
    MappingDecision,
    MappingSummary,
    SuggestRequest,
    SuggestRunRead,
)
from app.services.comparison import Classification, FieldComparison, compare, field_comparison_dict
from app.services.profiling.types import DatasetProfile
from app.services.sources import read_document, stored_profiles
from app.target.catalog import TargetField, catalog_by_path, target_field_catalog

log = get_logger(__name__)

PURPOSE_SUGGEST = "mapping_suggest"
DECIDED = (MappingStatus.approved, MappingStatus.rejected, MappingStatus.ignored)


# --- proposals, with or without a model -------------------------------------------------------


@dataclass
class Draft:
    """one proposed mapping before it becomes a row. produced by the model or the heuristic."""

    source_field: str
    target: str | None
    reason: str
    confidence: float | None = None
    transformation_required: bool = False
    transformation: str | None = None
    clarification_required: bool = False
    question: str | None = None


@dataclass
class ProposalOutcome:
    proposal: MappingProposal
    attempts: int
    last: StructuredResult[MappingProposal]
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    problems_seen: list[str] = field(default_factory=list)


def check_proposal(
    proposal: MappingProposal, profile: DatasetProfile, catalog_paths: set[str]
) -> list[str]:
    """the contract the schema alone cannot enforce. every problem is phrased so it can be sent
    back to the model verbatim."""
    problems: list[str] = []
    expected = [f.name for f in profile.fields]
    got = [m.source_field for m in proposal.mappings]
    missing = [n for n in expected if n not in got]
    unknown = [n for n in got if n not in expected]
    duplicates = sorted({n for n in got if got.count(n) > 1})
    if missing:
        problems.append(f"missing source fields: {', '.join(missing)}")
    if unknown:
        problems.append(f"source fields that do not exist in this dataset: {', '.join(unknown)}")
    if duplicates:
        problems.append(f"source fields answered more than once: {', '.join(duplicates)}")
    for m in proposal.mappings:
        if m.target_field is not None and m.target_field not in catalog_paths:
            problems.append(
                f"{m.source_field}: target_field '{m.target_field}' is not in the catalog; "
                "use an exact entity.field path or null"
            )
        if not 0.0 <= m.confidence <= 1.0:
            problems.append(f"{m.source_field}: confidence {m.confidence} is outside 0..1")
        if m.clarification_required and not (m.clarification_question or "").strip():
            problems.append(f"{m.source_field}: clarification_required is true but no question")
        if m.transformation_required and not (m.transformation or "").strip():
            problems.append(f"{m.source_field}: transformation_required is true but no rule")
    return problems


def propose(
    provider: LlmProvider,
    bundle: PromptBundle,
    *,
    profile: DatasetProfile,
    catalog_paths: set[str],
    settings: Settings | None = None,
) -> ProposalOutcome:
    """ask the model, check the answer, send the problems back a bounded number of times.
    no database here; the api and the eval runner both use this."""
    settings = settings or get_settings()
    attempts = 0
    feedback = ""
    tokens_in = tokens_out = 0
    latency = 0.0
    seen: list[str] = []
    while True:
        attempts += 1
        result: StructuredResult[MappingProposal] = provider.complete(
            system=bundle.system,
            user=bundle.user + feedback,
            output=MappingProposal,
            max_tokens=settings.llm_max_output_tokens,
        )
        tokens_in += result.input_tokens or 0
        tokens_out += result.output_tokens or 0
        latency += result.latency_ms
        problems = check_proposal(result.parsed, profile, catalog_paths)
        if not problems:
            return ProposalOutcome(
                proposal=result.parsed,
                attempts=attempts,
                last=result,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                latency_ms=round(latency, 1),
                problems_seen=seen,
            )
        seen.extend(problems)
        log.warning(
            "proposal_rejected",
            dataset=profile.name,
            attempt=attempts,
            problems=problems[:5],
            model=result.model,
        )
        if attempts >= settings.llm_max_attempts:
            raise LlmError(
                f"the model's proposal for {profile.name} failed validation {attempts} times",
                details={"problems": problems[:10], "attempts": attempts},
            )
        feedback = (
            "\n\nYour previous answer had these problems. Fix them and answer again in full:\n- "
            + "\n- ".join(problems)
        )


def drafts_from_proposal(proposal: MappingProposal) -> list[Draft]:
    return [
        Draft(
            source_field=m.source_field,
            target=m.target_field,
            reason=m.reason,
            confidence=round(float(m.confidence), 3),
            transformation_required=m.transformation_required,
            transformation=(m.transformation or None),
            clarification_required=m.clarification_required,
            question=(m.clarification_question or None),
        )
        for m in proposal.mappings
    ]


def drafts_from_comparison(
    profile: DatasetProfile, comparisons: dict[str, FieldComparison]
) -> list[Draft]:
    """manual mode: the deterministic comparison becomes the proposal. no confidence is
    invented; the origin says `heuristic` and the review screen shows it as such."""
    out: list[Draft] = []
    for f in profile.fields:
        fc = comparisons.get(f.name)
        if fc is None:
            out.append(Draft(f.name, None, "not compared"))
            continue
        cls = fc.classification
        best = fc.candidates[0] if fc.candidates else None
        if cls in (Classification.matched, Classification.transformation_required):
            out.append(
                Draft(
                    f.name,
                    fc.target,
                    fc.reason,
                    transformation_required=cls == Classification.transformation_required,
                    transformation="; ".join(best.notes) if best and best.notes else None,
                )
            )
        elif cls == Classification.ambiguous:
            candidates = [c.target for c in fc.candidates[:3]]
            values = list((f.stats.get("value_counts") or {}).keys())[:6] or None
            out.append(
                Draft(
                    f.name,
                    None,
                    fc.reason,
                    clarification_required=True,
                    question=clarification_template(f.name, values, candidates),
                )
            )
        else:
            out.append(Draft(f.name, None, fc.reason))
    return out


# --- suggesting -------------------------------------------------------------------------------


def _find_cached(session: Session, project_id: uuid.UUID, input_hash: str) -> LlmCall | None:
    return session.execute(
        select(LlmCall)
        .where(
            LlmCall.project_id == project_id,
            LlmCall.input_hash == input_hash,
            LlmCall.status == "ok",
            LlmCall.cached.is_(False),
            LlmCall.response.is_not(None),
        )
        .order_by(LlmCall.created_at.desc())
    ).scalar_one_or_none()


def _open_questions(mapping: FieldMapping) -> list[ClarificationQuestion]:
    return [q for q in mapping.questions if q.status == QuestionStatus.open]


def _withdraw_open_questions(mapping: FieldMapping, why: str) -> None:
    for q in _open_questions(mapping):
        q.status = QuestionStatus.withdrawn
        q.resolution = {"withdrawn": why}


def _question_context(draft: Draft, fc: FieldComparison | None, profile: DatasetProfile) -> dict:
    f = profile.field(draft.source_field)
    values = f.stats.get("value_counts")
    return {
        "proposed_target": draft.target,
        "confidence": draft.confidence,
        "candidates": [c.target for c in fc.candidates[:3]] if fc else [],
        "values": list(values)[:10] if values else None,
        "shapes": [x["shape"] for x in f.stats.get("shapes", [])[:3]],
        "inferred_type": f.inferred_type,
    }


def upsert_mappings(
    session: Session,
    project: OnboardingProject,
    dataset: SourceDataset,
    profile: DatasetProfile,
    drafts: list[Draft],
    comparisons: dict[str, FieldComparison],
    *,
    entity: str | None,
    origin: MappingOrigin,
    model: str | None,
    llm_call_id: uuid.UUID | None,
    force: bool,
) -> tuple[int, int, int]:
    """(written, kept, questions). decided rows and rows with an open question are kept unless
    `force`; suggested rows are replaced."""
    existing = {
        m.source_field: m
        for m in session.execute(
            select(FieldMapping)
            .where(FieldMapping.project_id == project.id, FieldMapping.dataset_id == dataset.id)
            .options(selectinload(FieldMapping.questions))
        ).scalars()
    }
    written = kept = questions = 0
    for draft in drafts:
        fc = comparisons.get(draft.source_field)
        try:
            fp = profile.field(draft.source_field)
        except KeyError:
            continue
        m = existing.get(draft.source_field)
        if m is not None and not force:
            if m.decided or (m.status == MappingStatus.needs_clarification and _open_questions(m)):
                kept += 1
                continue
        if m is None:
            m = FieldMapping(
                project_id=project.id, dataset_id=dataset.id, source_field=draft.source_field
            )
            session.add(m)
        elif force:
            _withdraw_open_questions(m, "re-suggested with force")
        m.inferred_type = fp.inferred_type
        m.entity = entity
        m.target_path = draft.target
        m.origin = origin
        m.confidence = draft.confidence
        m.reason = draft.reason
        m.transformation_required = draft.transformation_required
        m.transformation = draft.transformation
        m.clarification_required = draft.clarification_required
        m.comparison_class = fc.classification.value if fc else None
        m.candidates = [c.model_dump() for c in fc.candidates[:3]] if fc else []
        m.model = model
        m.llm_call_id = llm_call_id
        m.status = (
            MappingStatus.needs_clarification
            if draft.clarification_required
            else MappingStatus.suggested
        )
        m.decided_by = None
        m.decided_at = None
        m.decision_note = None
        if draft.clarification_required:
            candidates = [c.target for c in fc.candidates[:3]] if fc else []
            values = list((fp.stats.get("value_counts") or {}).keys())[:6] or None
            session.add(
                ClarificationQuestion(
                    project_id=project.id,
                    mapping=m,
                    dataset_name=dataset.name,
                    source_field=draft.source_field,
                    question=draft.question
                    or clarification_template(draft.source_field, values, candidates),
                    context=_question_context(draft, fc, profile),
                    origin=origin,
                )
            )
            questions += 1
        written += 1
    session.flush()
    return written, kept, questions


@dataclass
class _Job:
    """one dataset's way through a suggest run."""

    profile: DatasetProfile
    dataset: SourceDataset
    entity: str | None
    comparisons: dict[str, FieldComparison]
    bundle: PromptBundle | None = None
    hit: LlmCall | None = None
    call: LlmCall | None = None
    outcome: ProposalOutcome | None = None
    error: LlmError | None = None


def _run_proposals(
    provider: LlmProvider,
    jobs: list[_Job],
    *,
    catalog_paths: set[str],
    settings: Settings,
) -> None:
    """ask the model about several datasets at once. each thread gets a copy of the request's
    log context so its lines still carry the request id and project id."""

    def ask(job: _Job) -> ProposalOutcome:
        assert job.bundle is not None
        return propose(
            provider,
            job.bundle,
            profile=job.profile,
            catalog_paths=catalog_paths,
            settings=settings,
        )

    workers = max(1, min(settings.llm_parallel_calls, len(jobs)))
    if workers == 1:
        for job in jobs:
            try:
                job.outcome = ask(job)
            except LlmError as exc:
                job.error = exc
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(contextvars.copy_context().run, ask, job): job for job in jobs}
        for future, job in futures.items():
            try:
                job.outcome = future.result()
            except LlmError as exc:
                job.error = exc


def suggest_mappings(
    session: Session,
    project: OnboardingProject,
    *,
    provider: LlmProvider,
    body: SuggestRequest,
    settings: Settings | None = None,
) -> SuggestRunRead:
    settings = settings or get_settings()
    started = time.perf_counter()
    profiles = stored_profiles(session, project)
    if not profiles:
        raise InvalidStateError(
            "profile the sources before suggesting mappings",
            details={"project_id": str(project.id)},
        )
    names = {p.name for p in profiles}
    wanted = set(body.datasets) if body.datasets else names
    unknown = sorted(wanted - names)
    if unknown:
        raise NotFoundError("no profiled dataset with that name", details={"datasets": unknown})

    structlog.contextvars.bind_contextvars(project_id=str(project.id), stage="mapping")
    report = compare(profiles)  # every dataset together, so entity coverage is whole-project
    by_dataset = field_comparison_dict(report)
    catalog = target_field_catalog()
    catalog_paths = {f.path for f in catalog}
    rules_text = read_document(body.rules_document, settings) if body.rules_document else None
    datasets = {d.name: d for d in project.datasets}
    manual = provider.name == "none"

    # phase 1: decide per dataset whether the answer is cached, needs a model call, or comes from
    # the comparison. database work only, on this thread.
    jobs: list[_Job] = []
    for profile in profiles:
        if profile.name not in wanted:
            continue
        job = _Job(
            profile=profile,
            dataset=datasets[profile.name],
            entity=report.datasets.get(profile.name),
            comparisons=by_dataset.get(profile.name, {}),
        )
        if not manual:
            job.bundle = build_mapping_prompt(
                profile=profile,
                entity=job.entity,
                comparisons=job.comparisons,
                catalog=catalog,
                customer=project.customer_name,
                rules_text=rules_text,
                project_notes=project.notes,
                provider=provider.name,
                model=provider.model,
            )
            job.hit = (
                None if body.force else _find_cached(session, project.id, job.bundle.input_hash)
            )
            if job.hit is None:
                job.call = LlmCall(
                    project_id=project.id,
                    purpose=PURPOSE_SUGGEST,
                    dataset=profile.name,
                    provider=provider.name,
                    model=provider.model,
                    prompt_version=PROMPT_VERSION,
                    input_hash=job.bundle.input_hash,
                    cached=False,
                    status="error",
                    attempts=0,
                )
                session.add(job.call)
        jobs.append(job)
    session.flush()

    # phase 2: the model calls, in parallel. one call runs a minute or two; four datasets in
    # sequence would be the whole demo. successes are recorded even when a sibling fails, so a
    # retry only re-asks what failed.
    pending = [j for j in jobs if j.call is not None]
    if pending:
        _run_proposals(provider, pending, catalog_paths=catalog_paths, settings=settings)
        for job in pending:
            assert job.call is not None
            if job.error is not None:
                job.call.error = job.error.message
                job.call.attempts = int(job.error.details.get("attempts", 1))
            elif job.outcome is not None:
                job.call.status = "ok"
                job.call.model = job.outcome.last.model
                job.call.attempts = job.outcome.attempts
                job.call.input_tokens = job.outcome.input_tokens
                job.call.output_tokens = job.outcome.output_tokens
                job.call.latency_ms = job.outcome.latency_ms
                job.call.request_id = job.outcome.last.request_id
                job.call.response = job.outcome.proposal.model_dump()
                log.info(
                    "mapping_proposal",
                    dataset=job.profile.name,
                    model=job.call.model,
                    attempts=job.call.attempts,
                    input_tokens=job.call.input_tokens,
                    output_tokens=job.call.output_tokens,
                    duration_ms=job.call.latency_ms,
                )
        failed = [j for j in pending if j.error is not None]
        if failed:
            session.commit()
            if len(failed) == 1:
                job = failed[0]
                assert job.error is not None
                raise LlmError(
                    f"{job.profile.name}: {job.error.message}",
                    details={**job.error.details, "dataset": job.profile.name},
                )
            raise LlmError(
                f"the model's proposals failed for {len(failed)} datasets; the others are cached, "
                "run suggest again to re-ask only the failed ones",
                details={
                    "failed": {
                        j.profile.name: {**j.error.details, "message": j.error.message}
                        for j in failed
                        if j.error is not None
                    }
                },
            )
        session.flush()

    # phase 3: rows. decided rows are kept, suggested rows replaced, questions opened.
    results: list[DatasetSuggestResult] = []
    for job in jobs:
        cached = False
        llm_call_id: uuid.UUID | None = None
        observations: list[str] = []
        model: str | None = None
        if manual:
            drafts = drafts_from_comparison(job.profile, job.comparisons)
            origin = MappingOrigin.heuristic
        else:
            origin = MappingOrigin.model
            assert job.bundle is not None
            if job.hit is not None:
                proposal = MappingProposal.model_validate(job.hit.response)
                cached = True
                llm_call_id = job.hit.id
                model = job.hit.model
                session.add(
                    LlmCall(
                        project_id=project.id,
                        purpose=PURPOSE_SUGGEST,
                        dataset=job.profile.name,
                        provider=provider.name,
                        model=job.hit.model,
                        prompt_version=PROMPT_VERSION,
                        input_hash=job.bundle.input_hash,
                        cached=True,
                        status="ok",
                        attempts=0,
                        input_tokens=0,
                        output_tokens=0,
                        latency_ms=0.0,
                    )
                )
                log.info(
                    "mapping_proposal_cached", dataset=job.profile.name, llm_call_id=str(job.hit.id)
                )
            else:
                assert job.call is not None and job.outcome is not None
                proposal = job.outcome.proposal
                llm_call_id = job.call.id
                model = job.call.model
            drafts = drafts_from_proposal(proposal)
            observations = list(proposal.observations)

        written, kept, questions = upsert_mappings(
            session,
            project,
            job.dataset,
            job.profile,
            drafts,
            job.comparisons,
            entity=job.entity,
            origin=origin,
            model=model,
            llm_call_id=llm_call_id,
            force=body.force,
        )
        results.append(
            DatasetSuggestResult(
                dataset=job.profile.name,
                entity=job.entity,
                origin=origin,
                written=written,
                kept=kept,
                questions=questions,
                cached=cached,
                llm_call_id=llm_call_id,
                observations=observations,
            )
        )

    advance_stage(session, project)
    session.commit()
    counts = status_counts(session, project)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    log.info(
        "mappings_suggested",
        datasets=len(results),
        provider=provider.name,
        counts=counts,
        duration_ms=duration_ms,
    )
    return SuggestRunRead(
        project_id=project.id,
        stage=project.stage,
        provider=provider.name,
        model=provider.model or None,
        prompt_version=PROMPT_VERSION,
        datasets=results,
        counts=counts,
        duration_ms=duration_ms,
    )


# --- reading ----------------------------------------------------------------------------------


def _query(project: OnboardingProject):
    return (
        select(FieldMapping)
        .where(FieldMapping.project_id == project.id)
        .options(selectinload(FieldMapping.dataset), selectinload(FieldMapping.questions))
    )


def list_mappings(
    session: Session,
    project: OnboardingProject,
    *,
    dataset: str | None = None,
    status: MappingStatus | None = None,
) -> list[FieldMapping]:
    q = _query(project)
    if dataset:
        q = q.join(FieldMapping.dataset).where(SourceDataset.name == dataset)
    if status:
        q = q.where(FieldMapping.status == status)
    rows = list(session.execute(q).scalars())
    # source column order, so the review screen reads like the file does
    positions = {(f.dataset_id, f.name): f.position for d in project.datasets for f in d.fields}
    rows.sort(key=lambda m: (m.dataset.name, positions.get((m.dataset_id, m.source_field), 0)))
    return rows


def get_mapping(
    session: Session, project: OnboardingProject, mapping_id: uuid.UUID
) -> FieldMapping:
    m = session.execute(_query(project).where(FieldMapping.id == mapping_id)).scalar_one_or_none()
    if m is None:
        raise NotFoundError(
            f"mapping {mapping_id} not found on this project",
            details={"project_id": str(project.id), "mapping_id": str(mapping_id)},
        )
    return m


def status_counts(session: Session, project: OnboardingProject) -> dict[str, int]:
    counts = {s.value: 0 for s in MappingStatus}
    for m in session.execute(
        select(FieldMapping).where(FieldMapping.project_id == project.id)
    ).scalars():
        counts[m.status.value] += 1
    return counts


def summary(
    session: Session, project: OnboardingProject, settings: Settings | None = None
) -> MappingSummary:
    settings = settings or get_settings()
    rows = list_mappings(session, project)
    counts = {s.value: 0 for s in MappingStatus}
    by_dataset: dict[str, dict[str, int]] = {}
    high = low = 0
    approved_targets: dict[str, set[str]] = {}
    pending_targets: dict[str, set[str]] = {}
    entities_in_play: set[str] = set()
    for m in rows:
        counts[m.status.value] += 1
        by_dataset.setdefault(m.dataset.name, {s.value: 0 for s in MappingStatus})
        by_dataset[m.dataset.name][m.status.value] += 1
        if m.entity:
            entities_in_play.add(m.entity)
        pending = m.status in (MappingStatus.suggested, MappingStatus.needs_clarification)
        if pending and m.confidence is not None:
            if (
                m.status == MappingStatus.suggested
                and m.target_path
                and m.confidence >= settings.mapping_high_confidence
            ):
                high += 1
            if m.confidence < settings.mapping_low_confidence:
                low += 1
        if m.target_path:
            entity = m.target_path.split(".", 1)[0]
            if m.status == MappingStatus.approved:
                approved_targets.setdefault(entity, set()).add(m.target_path)
            elif pending:
                pending_targets.setdefault(entity, set()).add(m.target_path)
    open_questions = sum(
        1
        for q in session.execute(
            select(ClarificationQuestion).where(
                ClarificationQuestion.project_id == project.id,
                ClarificationQuestion.status == QuestionStatus.open,
            )
        ).scalars()
    )
    coverage: list[EntityMappingCoverage] = []
    catalog = target_field_catalog()
    for entity in sorted(entities_in_play | set(approved_targets) | set(pending_targets)):
        required = [t.path for t in catalog if t.entity == entity and t.required]
        approved = approved_targets.get(entity, set())
        pending_set = pending_targets.get(entity, set())
        coverage.append(
            EntityMappingCoverage(
                entity=entity,
                required=required,
                approved=[r for r in required if r in approved],
                pending=[r for r in required if r not in approved and r in pending_set],
                missing=[r for r in required if r not in approved and r not in pending_set],
            )
        )
    return MappingSummary(
        project_id=project.id,
        stage=project.stage,
        total=len(rows),
        counts=counts,
        by_dataset=by_dataset,
        open_questions=open_questions,
        high_confidence_pending=high,
        low_confidence_pending=low,
        thresholds={
            "high": settings.mapping_high_confidence,
            "low": settings.mapping_low_confidence,
        },
        coverage=coverage,
    )


# --- deciding ---------------------------------------------------------------------------------


def _require_target(target: str | None, catalog: dict[str, TargetField]) -> str:
    if not target:
        raise AppError(
            "a target_path is needed to approve",
            status_code=422,
            error_type="target_required",
        )
    if target not in catalog:
        raise AppError(
            f"{target} is not a target field",
            status_code=422,
            error_type="unknown_target",
            details={"target_path": target, "hint": "use entity.field from the catalog"},
        )
    return target


def _mark_decided(m: FieldMapping, who: str, note: str | None) -> None:
    m.decided_by = who
    m.decided_at = datetime.now(UTC)
    if note is not None:
        m.decision_note = note


def _clear_decision(m: FieldMapping) -> None:
    m.decided_by = None
    m.decided_at = None


def decide(
    session: Session,
    project: OnboardingProject,
    mapping_id: uuid.UUID,
    decision: MappingDecision,
    *,
    settings: Settings | None = None,
) -> FieldMapping:
    m = get_mapping(session, project, mapping_id)
    catalog = catalog_by_path()
    action = decision.action

    if action == "approve":
        target = _require_target(decision.target_path or m.target_path, catalog)
        if target != m.target_path:
            m.target_path = target
            m.origin = MappingOrigin.manual
        if decision.transformation is not None:
            m.transformation = decision.transformation or None
            m.transformation_required = bool(decision.transformation)
        m.clarification_required = False
        m.status = MappingStatus.approved
        _mark_decided(m, decision.reviewer, decision.note)
        _withdraw_open_questions(m, f"mapping approved by {decision.reviewer}")
    elif action == "reject":
        m.status = MappingStatus.rejected
        m.clarification_required = False
        _mark_decided(m, decision.reviewer, decision.note)
        _withdraw_open_questions(m, f"mapping rejected by {decision.reviewer}")
    elif action == "ignore":
        m.status = MappingStatus.ignored
        m.clarification_required = False
        _mark_decided(m, decision.reviewer, decision.note)
        _withdraw_open_questions(m, f"field ignored by {decision.reviewer}")
    elif action == "edit":
        if decision.target_path is not None:
            m.target_path = _require_target(decision.target_path, catalog)
        if decision.transformation is not None:
            m.transformation = decision.transformation or None
            m.transformation_required = bool(decision.transformation)
        m.origin = MappingOrigin.manual
        if decision.note is not None:
            m.decision_note = decision.note
    elif action == "clarify":
        values = None
        question = decision.question or clarification_template(
            m.source_field, values, [c["target"] for c in m.candidates[:3]]
        )
        session.add(
            ClarificationQuestion(
                project_id=project.id,
                mapping=m,
                dataset_name=m.dataset.name,
                source_field=m.source_field,
                question=question,
                context={
                    "proposed_target": m.target_path,
                    "confidence": m.confidence,
                    "candidates": [c["target"] for c in m.candidates[:3]],
                    "asked_by": decision.reviewer,
                },
                origin=MappingOrigin.manual,
            )
        )
        m.status = MappingStatus.needs_clarification
        m.clarification_required = True
        _clear_decision(m)
        if decision.note is not None:
            m.decision_note = decision.note
    elif action == "reopen":
        m.status = MappingStatus.suggested
        m.clarification_required = False
        _clear_decision(m)
        _withdraw_open_questions(m, f"mapping reopened by {decision.reviewer}")
        if decision.note is not None:
            m.decision_note = decision.note

    advance_stage(session, project)
    session.commit()
    session.refresh(m)
    log.info(
        "mapping_decided",
        project_id=str(project.id),
        mapping_id=str(m.id),
        dataset=m.dataset.name,
        source_field=m.source_field,
        action=action,
        status=m.status.value,
        target=m.target_path,
        reviewer=decision.reviewer,
    )
    return m


def bulk_approve(
    session: Session,
    project: OnboardingProject,
    *,
    min_confidence: float | None,
    reviewer: str,
    settings: Settings | None = None,
) -> BulkApproveResult:
    """approve every suggested mapping that has a target and a confidence at or above the
    threshold. the configured high threshold is a floor the caller cannot go under."""
    settings = settings or get_settings()
    threshold = max(min_confidence or 0.0, settings.mapping_high_confidence)
    approved = skipped = 0
    for m in list_mappings(session, project, status=MappingStatus.suggested):
        if not m.target_path:
            continue
        if m.confidence is not None and m.confidence >= threshold and not m.clarification_required:
            m.status = MappingStatus.approved
            _mark_decided(m, reviewer, f"bulk approved at confidence >= {threshold}")
            approved += 1
        else:
            skipped += 1
    advance_stage(session, project)
    session.commit()
    log.info(
        "mappings_bulk_approved",
        project_id=str(project.id),
        approved=approved,
        skipped=skipped,
        threshold=threshold,
    )
    return BulkApproveResult(
        approved=approved, skipped=skipped, min_confidence=threshold, stage=project.stage
    )


# --- stage ------------------------------------------------------------------------------------

_LATER_THAN_MAPPING = (
    ProjectStage.validated,
    ProjectStage.dry_run_complete,
    ProjectStage.reported,
)


def advance_stage(session: Session, project: OnboardingProject) -> None:
    """mapped: proposals exist. in_review: a person has started deciding. ready_to_transform:
    every field decided and no open question. later stages are only pulled back when a
    mapping is reopened."""
    rows = list(
        session.execute(select(FieldMapping).where(FieldMapping.project_id == project.id)).scalars()
    )
    if not rows:
        return
    open_q = session.execute(
        select(ClarificationQuestion.id).where(
            ClarificationQuestion.project_id == project.id,
            ClarificationQuestion.status == QuestionStatus.open,
        )
    ).first()
    all_decided = all(m.decided for m in rows)
    person_acted = any(m.decided for m in rows)
    if all_decided and open_q is None:
        computed = ProjectStage.ready_to_transform
    elif person_acted:
        computed = ProjectStage.in_review
    else:
        # proposals only, even with questions the model raised: nobody has reviewed yet
        computed = ProjectStage.mapped
    if project.stage in _LATER_THAN_MAPPING and computed == ProjectStage.ready_to_transform:
        return
    if project.stage != computed:
        log.info("stage_changed", from_stage=project.stage.value, to_stage=computed.value)
        project.stage = computed


def list_llm_calls(session: Session, project: OnboardingProject) -> list[LlmCall]:
    return list(
        session.execute(
            select(LlmCall)
            .where(LlmCall.project_id == project.id)
            .order_by(LlmCall.created_at.desc())
        ).scalars()
    )


def drafts_for_eval(proposal: MappingProposal) -> list[dict[str, Any]]:
    """flat rows for the eval runner and the cli."""
    return [
        {
            "source_field": m.source_field,
            "target_field": m.target_field,
            "confidence": m.confidence,
            "clarification_required": m.clarification_required,
            "transformation_required": m.transformation_required,
            "reason": m.reason,
        }
        for m in proposal.mappings
    ]


__all__ = [
    "Draft",
    "ProposalOutcome",
    "SuggestedMapping",
    "advance_stage",
    "bulk_approve",
    "check_proposal",
    "decide",
    "drafts_from_comparison",
    "drafts_from_proposal",
    "get_mapping",
    "list_llm_calls",
    "list_mappings",
    "propose",
    "status_counts",
    "suggest_mappings",
    "summary",
    "upsert_mappings",
]
