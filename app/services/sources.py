"""attach the customer's sources to a project, profile them, compare them with the target."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ConflictError, InvalidStateError, NotFoundError
from app.core.logging import get_logger, stage
from app.models.project import (
    DatasetKind,
    OnboardingProject,
    ProjectStage,
    SourceDataset,
    SourceField,
)
from app.schemas.mapping import AvailableDocument
from app.schemas.sources import AvailableSource, ProfiledDataset, ProfileRunRead, SourceAttach
from app.services.comparison import ComparisonReport, compare
from app.services.profiling import (
    DatasetProfile,
    FieldProfile,
    assess,
    infer_references,
    load_source,
    profile_frame,
    resolve_source_path,
)

log = get_logger(__name__)

_SKIP_FILES = {"manifest.json"}
_DOC_SUFFIXES = {".md", ".txt"}
DOCUMENT_MAX_CHARS = 60_000


def available_documents(settings: Settings | None = None) -> list[AvailableDocument]:
    """markdown and text files under the document roots: the customer's rules, kickoff notes."""
    settings = settings or get_settings()
    out: list[AvailableDocument] = []
    for root in settings.document_root_paths:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for file in sorted(root_path.rglob("*")):
            if file.is_file() and file.suffix.lower() in _DOC_SUFFIXES:
                out.append(
                    AvailableDocument(
                        name=file.name, location=file.as_posix(), size_bytes=file.stat().st_size
                    )
                )
    return out


def read_document(location: str, settings: Settings | None = None) -> str:
    """a context document, path-checked against the document roots, size-capped."""
    settings = settings or get_settings()
    path = resolve_source_path(location, settings.document_root_paths)
    if path.suffix.lower() not in _DOC_SUFFIXES:
        raise AppError(
            "context documents must be .md or .txt",
            status_code=422,
            error_type="invalid_location",
            details={"location": location},
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > DOCUMENT_MAX_CHARS:
        text = text[:DOCUMENT_MAX_CHARS] + f"\n\n[truncated at {DOCUMENT_MAX_CHARS} characters]"
    return text


def available_sources(settings: Settings | None = None) -> list[AvailableSource]:
    """files under the source roots plus the billing feed, ready to attach."""
    settings = settings or get_settings()
    out: list[AvailableSource] = []
    for root in settings.source_root_paths:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for file in sorted(root_path.rglob("*")):
            if not file.is_file() or file.name in _SKIP_FILES:
                continue
            suffix = file.suffix.lower().lstrip(".")
            if suffix not in ("csv", "json"):
                continue
            out.append(
                AvailableSource(
                    name=file.name,
                    kind=DatasetKind(suffix),
                    location=file.as_posix(),
                    size_bytes=file.stat().st_size,
                )
            )
    out.append(
        AvailableSource(
            name="subscriptions (billing api)",
            kind=DatasetKind.api,
            location=f"{settings.billing_api_base_url.rstrip('/')}/subscriptions",
            system="LegacyBill 4.2",
        )
    )
    return out


def attach_source(
    session: Session, project: OnboardingProject, data: SourceAttach
) -> SourceDataset:
    if data.kind == DatasetKind.api:
        if not data.location.startswith(("http://", "https://")):
            raise AppError(
                "an api source needs an http(s) url",
                status_code=422,
                error_type="invalid_location",
                details={"location": data.location},
            )
    else:
        resolve_source_path(data.location)  # raises for traversal, outside roots, missing
    if any(d.name == data.name for d in project.datasets):
        raise ConflictError(
            f"project already has a source named {data.name}", details={"name": data.name}
        )
    dataset = SourceDataset(
        project_id=project.id, name=data.name, kind=data.kind, location=data.location
    )
    session.add(dataset)
    session.commit()
    session.refresh(dataset)
    log.info(
        "source_attached",
        project_id=str(project.id),
        dataset=dataset.name,
        kind=dataset.kind.value,
    )
    return dataset


def get_source(
    session: Session, project: OnboardingProject, dataset_id: uuid.UUID
) -> SourceDataset:
    dataset = session.execute(
        select(SourceDataset)
        .where(SourceDataset.id == dataset_id, SourceDataset.project_id == project.id)
        .options(selectinload(SourceDataset.fields))
    ).scalar_one_or_none()
    if dataset is None:
        raise NotFoundError(
            f"source {dataset_id} not found on this project",
            details={"project_id": str(project.id), "dataset_id": str(dataset_id)},
        )
    return dataset


def delete_source(session: Session, project: OnboardingProject, dataset_id: uuid.UUID) -> None:
    dataset = get_source(session, project, dataset_id)
    session.delete(dataset)
    session.commit()
    log.info("source_detached", project_id=str(project.id), dataset=dataset.name)


def profile_project(
    session: Session,
    project: OnboardingProject,
    *,
    http_client: httpx.Client | None = None,
    settings: Settings | None = None,
) -> ProfileRunRead:
    """load and profile every attached source, then check keys across them. re-runnable."""
    with stage("profile", project_id=str(project.id)) as report:
        result = _profile_project(session, project, http_client=http_client, settings=settings)
        report.record_count = sum(d.row_count for d in result.datasets)
        report.note(datasets=len(result.datasets))
        return result


def _profile_project(
    session: Session,
    project: OnboardingProject,
    *,
    http_client: httpx.Client | None,
    settings: Settings | None,
) -> ProfileRunRead:
    settings = settings or get_settings()
    if not project.datasets:
        raise InvalidStateError(
            "attach at least one source before profiling", details={"project_id": str(project.id)}
        )
    started = time.perf_counter()
    frames = {}
    profiles: dict[str, DatasetProfile] = {}
    for dataset in project.datasets:
        t0 = time.perf_counter()
        df = load_source(dataset.kind, dataset.location, settings=settings, http_client=http_client)
        profile = profile_frame(df, name=dataset.name)
        frames[dataset.name] = df
        profiles[dataset.name] = profile
        log.info(
            "dataset_loaded",
            dataset=dataset.name,
            kind=dataset.kind.value,
            record_count=profile.row_count,
            column_count=profile.column_count,
            duration_ms=round((time.perf_counter() - t0) * 1000, 1),
        )

    references = infer_references(frames, profiles)
    now = datetime.now(UTC)
    results: list[ProfiledDataset] = []
    for dataset in project.datasets:
        profile = profiles[dataset.name]
        profile.references = references[dataset.name]
        profile.issues = assess(profile)
        dataset.row_count = profile.row_count
        dataset.column_count = profile.column_count
        dataset.profiled_at = now
        dataset.quality = profile.summary()
        session.execute(delete(SourceField).where(SourceField.dataset_id == dataset.id))
        for f in profile.fields:
            session.add(
                SourceField(
                    dataset_id=dataset.id,
                    name=f.name,
                    position=f.position,
                    inferred_type=f.inferred_type,
                    null_pct=f.null_pct,
                    unique_pct=f.unique_pct,
                    distinct_count=f.distinct_count,
                    sample_values=f.sample_values,
                    stats={**f.stats, "null_count": f.null_count},
                )
            )
        counts = profile.issue_counts()
        log.info(
            "dataset_profiled",
            dataset=dataset.name,
            record_count=profile.row_count,
            key_column=profile.key_column,
            errors=counts["error"],
            warnings=counts["warning"],
            duration_ms=profile.duration_ms,
        )
        results.append(
            ProfiledDataset(
                id=dataset.id,
                name=dataset.name,
                kind=dataset.kind,
                row_count=profile.row_count,
                column_count=profile.column_count,
                key_column=profile.key_column,
                issues=counts,
                duration_ms=profile.duration_ms,
            )
        )

    if project.stage in (ProjectStage.created, ProjectStage.profiled):
        project.stage = ProjectStage.profiled
    else:
        log.warning("reprofiled_after_mapping", stage=project.stage.value)
    session.commit()
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    log.info("project_profiled", datasets=len(results), duration_ms=duration_ms)
    return ProfileRunRead(
        project_id=project.id,
        stage=project.stage,
        profiled_at=now,
        duration_ms=duration_ms,
        datasets=results,
    )


def stored_profiles(session: Session, project: OnboardingProject) -> list[DatasetProfile]:
    """rebuild DatasetProfile objects from what profiling wrote to the database."""
    datasets = session.execute(
        select(SourceDataset)
        .where(SourceDataset.project_id == project.id, SourceDataset.profiled_at.is_not(None))
        .options(selectinload(SourceDataset.fields))
        .order_by(SourceDataset.name)
    ).scalars()
    out: list[DatasetProfile] = []
    for dataset in datasets:
        quality = dict(dataset.quality or {})
        quality.pop("fields", None)
        quality.pop("issue_counts", None)
        fields = [
            FieldProfile(
                name=f.name,
                position=f.position,
                inferred_type=f.inferred_type,
                null_count=int(f.stats.get("null_count", 0)),
                null_pct=f.null_pct,
                unique_pct=f.unique_pct,
                distinct_count=f.distinct_count,
                sample_values=f.sample_values,
                stats={k: v for k, v in f.stats.items() if k != "null_count"},
            )
            for f in dataset.fields
        ]
        out.append(DatasetProfile(**{**quality, "name": dataset.name, "fields": fields}))
    return out


def comparison_report(session: Session, project: OnboardingProject) -> ComparisonReport:
    profiles = stored_profiles(session, project)
    if not profiles:
        raise InvalidStateError(
            "profile the sources before comparing schemas", details={"project_id": str(project.id)}
        )
    return compare(profiles)
