"""set a project up the way a reviewer would have left it, without calling a model.

The golden mapping set in `evals/expected_mappings.json` is the right answer for the committed
sample customer: which target each column belongs in, and which columns have no home in Meridian.
Approving exactly that set gives the later stages a realistic starting point that does not depend
on a model being configured, and makes a failure in transformation or validation unambiguous.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.mapping import FieldMapping, MappingOrigin, MappingStatus
from app.models.project import OnboardingProject
from app.schemas.project import ProjectCreate
from app.schemas.sources import SourceAttach
from app.services import mapping as mapping_service
from app.services import sources as source_service
from app.services.projects import create_project

GOLDEN_PATH = Path("evals/expected_mappings.json")
SAMPLE_DATASETS = ("organizations.csv", "contacts.csv", "subscriptions.json", "activity.csv")


def golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text())


def golden_targets(dataset: str) -> dict[str, str | None]:
    """source field to the target it should land in, None where it has no home."""
    fields = golden()["datasets"][dataset]["fields"]
    return {name: info.get("expected") for name, info in fields.items()}


def seed_project(
    session: Session,
    *,
    datasets: tuple[str, ...] = SAMPLE_DATASETS,
    customer: str = "Apex Equipment Services",
) -> OnboardingProject:
    project = create_project(
        session,
        ProjectCreate(
            customer_name=customer,
            project_name="Apex onboarding",
            source_systems=["Legacy CRM", "LegacyBill 4.2"],
        ),
    )
    catalog = {d.name: d for d in source_service.available_sources()}
    for name in datasets:
        available = catalog[name]
        source_service.attach_source(
            session,
            project,
            SourceAttach(name=name, kind=available.kind, location=available.location),
        )
    session.commit()
    # the relationship was loaded before the datasets existed
    session.refresh(project)
    return project


def profile(session: Session, project: OnboardingProject, http_client: Any | None = None) -> None:
    source_service.profile_project(session, project, http_client=http_client)
    session.commit()
    session.refresh(project)


def approve_golden_mappings(
    session: Session, project: OnboardingProject, *, reviewer: str = "golden set"
) -> dict[str, int]:
    """write the golden answers straight in as decided mappings: approved where there is a target,
    ignored where the column has no home in Meridian."""
    now = datetime.now(UTC)
    counts = {"approved": 0, "ignored": 0}
    for dataset in project.datasets:
        targets = golden_targets(dataset.name)
        for source_field in dataset.fields:
            target = targets.get(source_field.name)
            approved = target is not None
            session.add(
                FieldMapping(
                    id=uuid.uuid4(),
                    project_id=project.id,
                    dataset_id=dataset.id,
                    source_field=source_field.name,
                    inferred_type=source_field.inferred_type,
                    entity=(target or "").split(".", 1)[0] or None,
                    target_path=target,
                    status=MappingStatus.approved if approved else MappingStatus.ignored,
                    origin=MappingOrigin.manual,
                    reason="from the golden mapping set",
                    decided_by=reviewer,
                    decided_at=now,
                )
            )
            counts["approved" if approved else "ignored"] += 1
    session.flush()
    mapping_service.advance_stage(session, project)
    session.commit()
    return counts


def ready_project(
    session: Session, *, datasets: tuple[str, ...] = SAMPLE_DATASETS, http_client: Any = None
) -> OnboardingProject:
    """a project profiled and fully reviewed, ready for transformation."""
    project = seed_project(session, datasets=datasets)
    profile(session, project, http_client=http_client)
    approve_golden_mappings(session, project)
    session.refresh(project)
    return project
