"""the lifecycle table: every event from every stage, and the rule that nothing else moves a
project."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from app.models.project import OnboardingProject, ProjectStage
from app.services import workflow
from app.services.workflow import Event

S = ProjectStage


def project_at(stage: ProjectStage) -> OnboardingProject:
    return OnboardingProject(id=uuid.uuid4(), customer_name="Apex", project_name="t", stage=stage)


@pytest.mark.parametrize(
    ("event", "start", "end"),
    [
        (Event.profiled, S.created, S.profiled),
        (Event.profiled, S.profiled, S.profiled),
        (Event.profiled, S.in_review, S.in_review),  # re-profiling keeps the review
        (Event.validated, S.ready_to_transform, S.validated),
        (Event.validated, S.in_review, S.in_review),  # not reviewed, not validated
        (Event.validated, S.dry_run_complete, S.dry_run_complete),  # no step backwards
        (Event.rehearsed, S.ready_to_transform, S.dry_run_complete),
        (Event.rehearsed, S.validated, S.dry_run_complete),
        (Event.rehearsed, S.reported, S.reported),
        (Event.reported, S.dry_run_complete, S.reported),
        (Event.reported, S.validated, S.validated),  # a report without a rehearsal moves nothing
    ],
)
def test_each_completion_moves_the_project_only_from_its_stages(event, start, end) -> None:
    project = project_at(start)
    moved = workflow.complete(project, event)
    assert project.stage is end
    assert moved == (start in workflow.COMPLETIONS[event][0])


@pytest.mark.parametrize(
    ("any_decided", "all_decided", "open_questions", "expected"),
    [
        (False, False, False, S.mapped),
        (False, False, True, S.mapped),  # the model asking is not a review
        (True, False, False, S.in_review),
        (True, True, True, S.in_review),  # decided, but the customer still owes an answer
        (True, True, False, S.ready_to_transform),
    ],
)
def test_the_review_stage_comes_from_the_mappings(
    any_decided, all_decided, open_questions, expected
) -> None:
    project = project_at(S.mapped)
    workflow.reviewed(
        project, any_decided=any_decided, all_decided=all_decided, open_questions=open_questions
    )
    assert project.stage is expected


def test_a_reopened_mapping_pulls_a_rehearsed_project_back_into_review() -> None:
    project = project_at(S.dry_run_complete)
    workflow.reviewed(project, any_decided=True, all_decided=False, open_questions=False)
    assert project.stage is S.in_review


def test_finishing_the_review_again_does_not_undo_a_rehearsal() -> None:
    for stage in workflow.PAST_REVIEW:
        project = project_at(stage)
        workflow.reviewed(project, any_decided=True, all_decided=True, open_questions=False)
        assert project.stage is stage


def test_nothing_outside_the_workflow_module_assigns_a_stage() -> None:
    assignment = re.compile(r"\.stage\s*=(?!=)")
    offenders = [
        f"{path}:{number}"
        for path in Path("app").rglob("*.py")
        if path.name != "workflow.py"
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if assignment.search(line)
    ]
    assert offenders == []
