"""the two runs: validate, and rehearse the migration against the target api.

A validation pass transforms and checks, writes nothing anywhere, and answers "could this
customer migrate today". A dry run goes on to send every valid record to the target platform over
http, into a staging namespace of its own, and answers "what happens when we actually do it".

What makes the rehearsal worth running:

  real calls          one http request per record, with the platform's token, through the same
                      api a production migration would use. The target's rules (relationships,
                      identifier collisions, one primary contact, lifecycle) are enforced by the
                      target, not simulated here.
  safe retries        a 5xx, a timeout or a malformed answer is retried, because a write carries
                      its own identifier and a repeat of the same record is a no-op. A 409 or a
                      422 is the target's considered answer and is never retried.
  no wasted calls     a record whose organization the target refused is not attempted. It is
                      recorded as blocked, with the organization that blocked it.
  nothing silent      every refusal is stored with the status, the error type, the target's own
                      message and the request id, so a person can find the one record in a run
                      of ten thousand.
"""

from __future__ import annotations

import re
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.migration import (
    FailureStage,
    MigrationFailure,
    MigrationRun,
    RunKind,
    RunStatus,
    ValidationIssueRow,
)
from app.models.project import OnboardingProject, ProjectStage
from app.models.target import ID_COLUMN, WRITE_ORDER
from app.services.profiling.types import Severity
from app.services.reconciliation import (
    EntityLedger,
    first_errors,
    read_target_ids,
    reconcile_run,
)
from app.services.transform import get_config, transform_project
from app.services.transform.config import TransformationConfig
from app.services.transform.engine import TransformResult
from app.services.transform.types import RecordDraft, RecordIssue
from app.services.validation import ValidationOutcome, validate

log = get_logger(__name__)

PLURAL: dict[str, str] = {
    "organization": "organizations",
    "contact": "contacts",
    "subscription": "subscriptions",
    "activity": "activities",
}
RETRYABLE_STATUS = (408, 425, 429, 500, 502, 503, 504)
# the quoted values inside a normalization note, so notes aggregate into one line per kind
_QUOTED = re.compile(r"'[^']*'")


@dataclass
class RunOptions:
    """what a caller can change about a run, all of it recorded on the run row."""

    entities: tuple[str, ...] | None = None
    limit_per_entity: int | None = None
    stop_after_failures: int | None = None
    purge_namespace_after: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "entities": list(self.entities) if self.entities else None,
            "limit_per_entity": self.limit_per_entity,
            "stop_after_failures": self.stop_after_failures,
            "purge_namespace_after": self.purge_namespace_after,
        }


@dataclass
class WriteResult:
    outcome: str  # accepted | rejected | failed | blocked
    attempts: int = 1
    created: bool = False
    http_status: int | None = None
    error_type: str | None = None
    message: str | None = None
    request_id: str | None = None
    body: str | None = None


@dataclass
class EntityTally:
    attempted: int = 0
    accepted: int = 0
    created: int = 0
    unchanged: int = 0
    rejected: int = 0
    failed: int = 0
    blocked: int = 0
    retries: int = 0
    # valid records the run never reached: past a limit, or after it gave up on the entity
    not_attempted: int = 0
    # identifiers, kept for reconciliation and not for the run row
    accepted_ids: list[str] = field(default_factory=list)
    refused_ids: list[str] = field(default_factory=list)
    refusals: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, int]:
        return {
            "attempted": self.attempted,
            "accepted": self.accepted,
            "created": self.created,
            "unchanged": self.unchanged,
            "rejected": self.rejected,
            "failed": self.failed,
            "blocked": self.blocked,
            "retries": self.retries,
            "not_attempted": self.not_attempted,
        }


class TargetClient:
    """one record per request, with retries that are safe because writes are idempotent."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        settings: Settings,
        namespace: uuid.UUID,
        request_prefix: str,
    ) -> None:
        self.client = client
        self.settings = settings
        self.namespace = namespace
        self.request_prefix = request_prefix
        self.base_url = settings.target_api_base_url.rstrip("/")

    def _headers(self, record_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.target_api_token}",
            "X-Meridian-Namespace": str(self.namespace),
            "X-Request-Id": f"{self.request_prefix}-{record_id}",
        }

    def write(self, entity: str, payload: dict[str, Any], record_id: str) -> WriteResult:
        url = f"{self.base_url}/{PLURAL[entity]}"
        headers = self._headers(record_id)
        attempts = 0
        last = WriteResult(outcome="failed", error_type="not_attempted", message="no attempt made")
        while attempts < max(1, self.settings.target_max_attempts):
            attempts += 1
            try:
                response = self.client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self.settings.target_request_timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                last = WriteResult(
                    outcome="failed",
                    attempts=attempts,
                    error_type="timeout",
                    message=f"the target did not answer within "
                    f"{self.settings.target_request_timeout_seconds}s ({exc.__class__.__name__})",
                    request_id=headers["X-Request-Id"],
                )
            except httpx.HTTPError as exc:
                last = WriteResult(
                    outcome="failed",
                    attempts=attempts,
                    error_type="transport_error",
                    message=f"{exc.__class__.__name__}: {str(exc)[:200]}",
                    request_id=headers["X-Request-Id"],
                )
            else:
                last = self._read(response, attempts, headers["X-Request-Id"])
                if last.outcome != "failed":
                    return last
            if attempts < self.settings.target_max_attempts:
                time.sleep(self.settings.target_retry_backoff_seconds * attempts)
        return last

    def _read(self, response: httpx.Response, attempts: int, request_id: str) -> WriteResult:
        status = response.status_code
        if status in (200, 201):
            try:
                body = response.json()
            except ValueError:
                body = None
            if not isinstance(body, dict) or "created" not in body:
                return WriteResult(
                    outcome="failed",
                    attempts=attempts,
                    http_status=status,
                    error_type="malformed_response",
                    message="the target answered 200 with a body that is not a write result",
                    request_id=request_id,
                    body=response.text[:500],
                )
            return WriteResult(
                outcome="accepted",
                attempts=attempts,
                created=bool(body.get("created")),
                http_status=status,
                request_id=request_id,
            )
        error = {}
        try:
            error = (response.json() or {}).get("error", {})
        except ValueError:
            error = {}
        error_type = str(error.get("type") or f"http_{status}")
        message = str(error.get("message") or response.text[:200])
        if status in RETRYABLE_STATUS:
            return WriteResult(
                outcome="failed",
                attempts=attempts,
                http_status=status,
                error_type=error_type,
                message=message,
                request_id=request_id,
                body=response.text[:500],
            )
        return WriteResult(
            outcome="rejected",
            attempts=attempts,
            http_status=status,
            error_type=error_type,
            message=message,
            request_id=request_id,
            body=response.text[:500],
        )

    def counts(self) -> dict[str, int]:
        response = self.client.get(
            f"{self.base_url}/counts",
            headers={
                "Authorization": f"Bearer {self.settings.target_api_token}",
                "X-Meridian-Namespace": str(self.namespace),
            },
            timeout=self.settings.target_request_timeout_seconds,
        )
        response.raise_for_status()
        return dict(response.json().get("counts", {}))

    def list_ids(self, entity: str, page_size: int = 1000) -> set[str]:
        """every identifier of one entity in the namespace, paged through the target's own
        read api. reconciliation trusts what the target says it holds, not what the run says
        it sent."""
        ids: set[str] = set()
        offset = 0
        column = ID_COLUMN[entity]
        while True:
            response = self.client.get(
                f"{self.base_url}/{PLURAL[entity]}",
                params={"limit": page_size, "offset": offset},
                headers={
                    "Authorization": f"Bearer {self.settings.target_api_token}",
                    "X-Meridian-Namespace": str(self.namespace),
                },
                timeout=self.settings.target_request_timeout_seconds,
            )
            response.raise_for_status()
            page = response.json()
            ids.update(str(row[column]) for row in page if row.get(column) is not None)
            if len(page) < page_size:
                return ids
            offset += page_size

    def purge(self) -> None:
        self.client.delete(
            f"{self.base_url}/namespaces/{self.namespace}",
            headers={"Authorization": f"Bearer {self.settings.target_api_token}"},
            timeout=self.settings.target_request_timeout_seconds,
        )


# --- what a run records -------------------------------------------------------------------------


def _note_pattern(note: str) -> str:
    """`created_on: date read as %m/%d/%Y` keeps its shape, the values inside it are dropped, so
    ten thousand notes aggregate into the handful of things normalization actually did."""
    return _QUOTED.sub("'...'", note)


def _normalization_counts(result: TransformResult, keep: int = 25) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for entity in result.plan.entities:
        for draft in result.records.get(entity, []):
            for note in draft.notes:
                counter[_note_pattern(note)] += 1
    return dict(counter.most_common(keep))


def _rule_counts(result: TransformResult) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for entity in result.plan.entities:
        for draft in result.records.get(entity, []):
            for rule in draft.applied_rules:
                counter[rule] += 1
            if draft.skipped and draft.skip_rule:
                counter[f"{draft.skip_rule}: records not migrated"] += 1
    return dict(counter.most_common())


def _store_issues(
    session: Session,
    run: MigrationRun,
    issues: list[RecordIssue],
    limit: int,
) -> bool:
    """errors and warnings first, so a truncated run keeps what matters."""
    order = {Severity.error: 0, Severity.warning: 1, Severity.info: 2}
    ordered = sorted(issues, key=lambda i: order[i.severity])
    for issue in ordered[:limit]:
        session.add(
            ValidationIssueRow(
                run_id=run.id,
                project_id=run.project_id,
                entity=issue.entity,
                dataset=issue.dataset,
                source_row=issue.source_row,
                record_id=issue.record_id,
                field=issue.field,
                error_type=issue.error_type,
                severity=issue.severity.value,
                message=issue.message[:2000],
                value=None if issue.value is None else issue.value[:300],
                rule=None if issue.rule is None else issue.rule[:200],
            )
        )
    return len(ordered) > limit


def _entity_stats(
    validation: ValidationOutcome, tallies: dict[str, EntityTally], source_rows: dict[str, int]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for entity, counts in validation.counts.items():
        row = dict(counts)
        row["source_rows"] = source_rows.get(entity, 0)
        if entity in tallies:
            row.update(tallies[entity].as_dict())
        out[entity] = row
    return out


def _totals(stats: dict[str, Any]) -> dict[str, int]:
    keys = (
        "source_rows",
        "built",
        "skipped_by_rule",
        "valid",
        "invalid",
        "with_warnings",
        "attempted",
        "accepted",
        "rejected",
        "failed",
        "blocked",
        "not_attempted",
    )
    return {key: sum(int(entity.get(key, 0)) for entity in stats.values()) for key in keys}


def _source_rows_by_entity(result: TransformResult) -> dict[str, int]:
    rows: dict[str, int] = defaultdict(int)
    for outcome in result.datasets:
        for entity in outcome.built:
            rows[entity] += outcome.source_rows
    return dict(rows)


def _start_run(
    session: Session,
    project: OnboardingProject,
    kind: RunKind,
    *,
    config: TransformationConfig,
    options: RunOptions,
    namespace: uuid.UUID | None,
) -> MigrationRun:
    run = MigrationRun(
        id=uuid.uuid4(),
        project_id=project.id,
        kind=kind,
        status=RunStatus.running,
        namespace=namespace,
        target_environment=project.target_environment,
        as_of=config.as_of,
        config_version=config.version,
        config=config.summary(),
        options=options.as_dict(),
    )
    session.add(run)
    session.commit()
    return run


def _finish(
    session: Session,
    run: MigrationRun,
    *,
    status: RunStatus,
    started: float,
    error: str | None = None,
) -> MigrationRun:
    run.status = status
    run.error = error
    run.finished_at = datetime.now(UTC)
    run.duration_ms = round((time.perf_counter() - started) * 1000, 1)
    session.commit()
    log.info(
        "run_finished",
        run_id=str(run.id),
        kind=run.kind.value,
        status=status.value,
        duration_ms=run.duration_ms,
        **{k: v for k, v in (run.totals or {}).items()},
    )
    return run


def _selected(result: TransformResult, options: RunOptions) -> tuple[str, ...]:
    entities = options.entities or result.plan.entities
    return tuple(e for e in WRITE_ORDER if e in entities and e in result.plan.entities)


def _ledgers(
    result: TransformResult,
    outcome: ValidationOutcome,
    tallies: dict[str, EntityTally],
    source_rows: dict[str, int],
) -> list[EntityLedger]:
    """what the run knows about every entity, for reconciliation. an entity the run did not
    write (restricted with `entities`) has every valid record counted as not reached."""
    ledgers = []
    for entity in WRITE_ORDER:
        if entity not in outcome.counts:
            continue
        counts = outcome.counts[entity]
        drafts = result.records.get(entity, [])
        tally = tallies.get(entity) or EntityTally(not_attempted=counts["valid"])
        ledgers.append(
            EntityLedger(
                entity=entity,
                source_rows=source_rows.get(entity, 0),
                distinct_ids=len({d.record_id for d in drafts if d.record_id}),
                built=counts["built"],
                skipped_by_rule=counts["skipped_by_rule"],
                excluded=counts["invalid"],
                excluded_by_reason=first_errors(drafts),
                valid=counts["valid"],
                not_attempted=tally.not_attempted,
                blocked=tally.blocked,
                attempted=tally.attempted,
                accepted=tally.accepted,
                rejected=tally.rejected,
                failed=tally.failed,
                refusals_by_type=dict(tally.refusals.most_common()),
                accepted_ids=list(tally.accepted_ids),
                refused_ids=list(tally.refused_ids),
            )
        )
    return ledgers


# --- validation ---------------------------------------------------------------------------------


def run_validation(
    session: Session,
    project: OnboardingProject,
    *,
    http_client: httpx.Client | None = None,
    settings: Settings | None = None,
    config: TransformationConfig | None = None,
    options: RunOptions | None = None,
) -> MigrationRun:
    """transform and check, write nothing. this is the gate a dry run runs behind."""
    settings = settings or get_settings()
    config = config or get_config()
    options = options or RunOptions()
    started = time.perf_counter()
    run = _start_run(
        session, project, RunKind.validation, config=config, options=options, namespace=None
    )
    structlog.contextvars.bind_contextvars(
        migration_run_id=str(run.id), project_id=str(project.id), stage="validation"
    )
    try:
        result = transform_project(
            session, project, config=config, settings=settings, http_client=http_client
        )
        outcome = validate(result, config=config)
    except Exception as exc:
        _finish(
            session,
            run,
            status=RunStatus.failed,
            started=started,
            error=f"{exc.__class__.__name__}: {exc}"[:1000],
        )
        raise
    run.plan = result.plan.as_dict()
    run.stats = _entity_stats(outcome, {}, _source_rows_by_entity(result))
    run.totals = _totals(run.stats)
    run.issue_counts = outcome.issue_counts()
    run.applied_rules = _rule_counts(result)
    run.normalizations = _normalization_counts(result)
    run.issues_truncated = _store_issues(
        session, run, outcome.issues, settings.validation_issue_limit
    )
    if project.stage in (ProjectStage.ready_to_transform, ProjectStage.validated):
        project.stage = ProjectStage.validated
    return _finish(session, run, status=RunStatus.completed, started=started)


# --- the dry run --------------------------------------------------------------------------------


def run_dry_run(
    session: Session,
    project: OnboardingProject,
    *,
    http_client: httpx.Client,
    settings: Settings | None = None,
    config: TransformationConfig | None = None,
    options: RunOptions | None = None,
) -> MigrationRun:
    """the rehearsal: every valid record through the target api, into a namespace of its own."""
    settings = settings or get_settings()
    config = config or get_config()
    options = options or RunOptions()
    started = time.perf_counter()
    run = _start_run(
        session,
        project,
        RunKind.dry_run,
        config=config,
        options=options,
        namespace=uuid.uuid4(),
    )
    namespace = run.namespace
    assert namespace is not None
    structlog.contextvars.bind_contextvars(
        migration_run_id=str(run.id),
        project_id=str(project.id),
        stage="dry_run",
        namespace=str(namespace),
    )
    target = TargetClient(
        http_client,
        settings=settings,
        namespace=namespace,
        request_prefix=f"dryrun-{str(run.id)[:8]}",
    )
    try:
        result = transform_project(
            session, project, config=config, settings=settings, http_client=http_client
        )
        outcome = validate(result, config=config)
        tallies, failures = _write_records(target, outcome, options)
    except Exception as exc:
        _finish(
            session,
            run,
            status=RunStatus.failed,
            started=started,
            error=f"{exc.__class__.__name__}: {exc}"[:1000],
        )
        raise

    run.plan = result.plan.as_dict()
    run.stats = _entity_stats(outcome, tallies, _source_rows_by_entity(result))
    run.totals = _totals(run.stats)
    run.issue_counts = outcome.issue_counts()
    run.applied_rules = _rule_counts(result)
    run.normalizations = _normalization_counts(result)
    run.issues_truncated = _store_issues(
        session, run, outcome.issues, settings.validation_issue_limit
    )
    kept = failures[: settings.migration_failure_limit]
    run.failures_truncated = len(failures) > len(kept)
    for failure in kept:
        failure.run_id = run.id
        session.add(failure)
    try:
        run.target_counts = target.counts()
    except httpx.HTTPError as exc:
        log.warning("target_counts_unavailable", error=str(exc)[:200])
        run.target_counts = {}
    # reconcile before any purge, while the namespace still holds what the run wrote
    source_rows = _source_rows_by_entity(result)
    ledgers = _ledgers(result, outcome, tallies, source_rows)
    reconcile_run(
        session,
        run,
        ledgers=ledgers,
        target_ids=read_target_ids(target, [ledger.entity for ledger in ledgers]),
    )
    if options.purge_namespace_after:
        target.purge()
        run.target_counts = {}
    else:
        log.info("staging_kept", namespace=str(namespace), counts=run.target_counts)
    if project.stage in (
        ProjectStage.ready_to_transform,
        ProjectStage.validated,
        ProjectStage.dry_run_complete,
    ):
        project.stage = ProjectStage.dry_run_complete
    return _finish(session, run, status=RunStatus.completed, started=started)


def _write_records(
    target: TargetClient, outcome: ValidationOutcome, options: RunOptions
) -> tuple[dict[str, EntityTally], list[MigrationFailure]]:
    """write in dependency order, and do not attempt a record whose organization was refused."""
    tallies: dict[str, EntityTally] = {}
    failures: list[MigrationFailure] = []
    # a child can only be written where its organization already landed in this namespace. an
    # organization that was refused, or that this run never attempted, blocks its children the
    # same way, and the message says which it was.
    accepted_organizations: set[str] = set()
    refused_organizations: dict[str, str] = {}
    entities = tuple(e for e in WRITE_ORDER if e in outcome.valid)
    if options.entities:
        entities = tuple(e for e in entities if e in options.entities)

    for entity in entities:
        tally = EntityTally()
        tallies[entity] = tally
        records: list[RecordDraft] = outcome.valid.get(entity, [])
        if options.limit_per_entity is not None:
            tally.not_attempted = max(0, len(records) - options.limit_per_entity)
            records = records[: options.limit_per_entity]
        for position, draft in enumerate(records):
            record_id = draft.record_id or ""
            parent = draft.payload.get("organization_id")
            if entity != "organization" and parent is not None:
                if str(parent) not in accepted_organizations:
                    refusal = refused_organizations.get(str(parent))
                    why = (
                        f"the target refused it ({refusal})"
                        if refusal
                        else "this run did not write it"
                    )
                    tally.blocked += 1
                    failures.append(
                        _failure(
                            draft,
                            entity,
                            stage=FailureStage.blocked,
                            error_type="parent_not_in_target",
                            message=f"organization {parent} is not in the target namespace: "
                            f"{why}, so this {entity} was not attempted",
                            attempts=0,
                        )
                    )
                    continue
            tally.attempted += 1
            answer = target.write(entity, draft.payload, record_id)
            tally.retries += max(0, answer.attempts - 1)
            if answer.outcome == "accepted":
                tally.accepted += 1
                tally.accepted_ids.append(record_id)
                if entity == "organization":
                    accepted_organizations.add(record_id)
                if answer.created:
                    tally.created += 1
                else:
                    tally.unchanged += 1
                continue
            if entity == "organization":
                refused_organizations[record_id] = answer.error_type or "refused"
            tally.refused_ids.append(record_id)
            tally.refusals[answer.error_type or "unknown"] += 1
            if answer.outcome == "rejected":
                tally.rejected += 1
                stage = FailureStage.target
            else:
                tally.failed += 1
                stage = FailureStage.target
            failures.append(
                _failure(
                    draft,
                    entity,
                    stage=stage,
                    error_type=answer.error_type or "unknown",
                    message=answer.message or "the target refused the record",
                    attempts=answer.attempts,
                    http_status=answer.http_status,
                    request_id=answer.request_id,
                    body=answer.body,
                )
            )
            if (
                options.stop_after_failures is not None
                and tally.rejected + tally.failed >= options.stop_after_failures
            ):
                tally.not_attempted += len(records) - position - 1
                log.warning(
                    "dry_run_stopped_early",
                    entity=entity,
                    failures=tally.rejected + tally.failed,
                    limit=options.stop_after_failures,
                )
                break
        log.info("entity_written", entity=entity, **tally.as_dict())
    return tallies, failures


def _failure(
    draft: RecordDraft,
    entity: str,
    *,
    stage: FailureStage,
    error_type: str,
    message: str,
    attempts: int,
    http_status: int | None = None,
    request_id: str | None = None,
    body: str | None = None,
) -> MigrationFailure:
    return MigrationFailure(
        entity=entity,
        record_id=draft.record_id,
        dataset=draft.dataset,
        source_row=draft.source_row,
        stage=stage,
        attempts=attempts,
        http_status=http_status,
        error_type=error_type[:64],
        message=message[:2000],
        request_id=request_id,
        response_excerpt=None if body is None else body[:500],
    )


# --- reading runs back --------------------------------------------------------------------------


def get_run(session: Session, project: OnboardingProject, run_id: uuid.UUID) -> MigrationRun:
    run = session.execute(
        select(MigrationRun).where(MigrationRun.id == run_id, MigrationRun.project_id == project.id)
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError(f"no run {run_id} on this project", details={"run_id": str(run_id)})
    return run


def list_runs(
    session: Session, project: OnboardingProject, *, kind: RunKind | None = None, limit: int = 50
) -> list[MigrationRun]:
    stmt = select(MigrationRun).where(MigrationRun.project_id == project.id)
    if kind is not None:
        stmt = stmt.where(MigrationRun.kind == kind)
    stmt = stmt.order_by(MigrationRun.started_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars())


def latest_run(
    session: Session, project: OnboardingProject, kind: RunKind | None = None
) -> MigrationRun | None:
    runs = list_runs(session, project, kind=kind, limit=1)
    return runs[0] if runs else None


def run_issues(
    session: Session,
    run: MigrationRun,
    *,
    entity: str | None = None,
    severity: str | None = None,
    error_type: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[ValidationIssueRow]:
    stmt = select(ValidationIssueRow).where(ValidationIssueRow.run_id == run.id)
    if entity:
        stmt = stmt.where(ValidationIssueRow.entity == entity)
    if severity:
        stmt = stmt.where(ValidationIssueRow.severity == severity)
    if error_type:
        stmt = stmt.where(ValidationIssueRow.error_type == error_type)
    stmt = stmt.order_by(
        ValidationIssueRow.severity, ValidationIssueRow.entity, ValidationIssueRow.record_id
    )
    return list(session.execute(stmt.limit(limit).offset(offset)).scalars())


def run_failures(
    session: Session,
    run: MigrationRun,
    *,
    entity: str | None = None,
    error_type: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[MigrationFailure]:
    stmt = select(MigrationFailure).where(MigrationFailure.run_id == run.id)
    if entity:
        stmt = stmt.where(MigrationFailure.entity == entity)
    if error_type:
        stmt = stmt.where(MigrationFailure.error_type == error_type)
    stmt = stmt.order_by(MigrationFailure.entity, MigrationFailure.record_id)
    return list(session.execute(stmt.limit(limit).offset(offset)).scalars())


def issue_breakdown(session: Session, run: MigrationRun) -> list[dict[str, Any]]:
    """counts by entity, severity and error type, for the workbench."""
    rows = session.execute(
        select(
            ValidationIssueRow.entity,
            ValidationIssueRow.severity,
            ValidationIssueRow.error_type,
            ValidationIssueRow.field,
        ).where(ValidationIssueRow.run_id == run.id)
    ).all()
    counter: Counter[tuple[str, str, str]] = Counter()
    fields: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    for entity, severity, error_type, field_name in rows:
        key = (entity, severity, error_type)
        counter[key] += 1
        if field_name:
            fields[key][field_name] += 1
    out = []
    for (entity, severity, error_type), count in counter.most_common():
        out.append(
            {
                "entity": entity,
                "severity": severity,
                "error_type": error_type,
                "count": count,
                "fields": [f for f, _ in fields[(entity, severity, error_type)].most_common(5)],
            }
        )
    return out


__all__ = [
    "EntityTally",
    "RunOptions",
    "TargetClient",
    "WriteResult",
    "get_run",
    "issue_breakdown",
    "latest_run",
    "list_runs",
    "run_dry_run",
    "run_failures",
    "run_issues",
    "run_validation",
]
