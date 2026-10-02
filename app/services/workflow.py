"""the project's lifecycle, in one place.

    created -> profiled -> mapped -> in_review -> ready_to_transform
            -> validated -> dry_run_complete -> reported

Nothing outside this module assigns `project.stage`. A service does its work, records it, and
tells this module what happened; the tables below decide whether the stage moves.

Two kinds of move:

  completions   profiling, a validation pass, a completed dry run and a stored report move the
                project forward, but only from the stages listed for them. From anywhere else
                the work is still recorded and the stage stays: re-profiling a project that is
                in review does not throw its review away, and a validation pass on a project
                that has already rehearsed does not pretend the rehearsal never happened.
  review        the mapping stage is computed from the mappings themselves: mapped while there
                are only proposals, in_review once a person has decided something,
                ready_to_transform when every field is decided and no question is open. A
                reopened mapping pulls a later stage back to in_review; finishing the review
                again does not undo a validation or a rehearsal that already happened.

This is the whole orchestration. Each stage is a person's action through the api, separated by
hours or days of review and customer answers, so the workflow's state is a column in postgres
next to the decisions it summarizes, not a graph held by a framework (see the LangGraph entry in
docs/BUILD_LOG.md).
"""

from __future__ import annotations

import enum

from app.core.logging import get_logger
from app.models.project import OnboardingProject, ProjectStage

log = get_logger(__name__)

S = ProjectStage


class Event(enum.StrEnum):
    """work that has finished and been recorded."""

    profiled = "profiled"
    validated = "validated"
    rehearsed = "rehearsed"
    reported = "reported"


# event: (the stages it may move the project from, the stage it moves it to)
COMPLETIONS: dict[Event, tuple[frozenset[ProjectStage], ProjectStage]] = {
    Event.profiled: (frozenset({S.created, S.profiled}), S.profiled),
    Event.validated: (frozenset({S.ready_to_transform, S.validated}), S.validated),
    Event.rehearsed: (
        frozenset({S.ready_to_transform, S.validated, S.dry_run_complete}),
        S.dry_run_complete,
    ),
    Event.reported: (frozenset({S.dry_run_complete, S.reported}), S.reported),
}

# stages past the review, which a finished review leaves alone
PAST_REVIEW = frozenset({S.validated, S.dry_run_complete, S.reported})


def complete(project: OnboardingProject, event: Event) -> bool:
    """move the project on because `event` happened. False when its stage is not one the event
    moves from; the work still counts, the stage just stays."""
    allowed, to = COMPLETIONS[event]
    if project.stage not in allowed:
        log.info("stage_kept", completed=event.value, stage=project.stage.value)
        return False
    _move(project, to, cause=event.value)
    return True


def review_stage(*, any_decided: bool, all_decided: bool, open_questions: bool) -> ProjectStage:
    """what the mappings say the stage is."""
    if all_decided and not open_questions:
        return S.ready_to_transform
    if any_decided:
        return S.in_review
    # proposals only, even with questions the model raised: nobody has reviewed yet
    return S.mapped


def reviewed(
    project: OnboardingProject, *, any_decided: bool, all_decided: bool, open_questions: bool
) -> None:
    """the mappings changed: proposals arrived, a person decided, a question opened or closed."""
    computed = review_stage(
        any_decided=any_decided, all_decided=all_decided, open_questions=open_questions
    )
    if project.stage in PAST_REVIEW and computed == S.ready_to_transform:
        return
    _move(project, computed, cause="review")


def _move(project: OnboardingProject, to: ProjectStage, *, cause: str) -> None:
    if project.stage == to:
        return
    log.info(
        "stage_changed",
        project_id=str(project.id),
        from_stage=project.stage.value,
        to_stage=to.value,
        cause=cause,
    )
    project.stage = to


__all__ = ["COMPLETIONS", "PAST_REVIEW", "Event", "complete", "review_stage", "reviewed"]
