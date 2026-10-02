"""the dry run's write loop when the target stops answering.

A target that refuses records is up and has an opinion; a target that fails record after record
is down. The breaker tells the two apart: failures in a row open it, a refusal or an acceptance
resets the count, and once it is open nothing more is sent and everything not reached is counted
as not reached, so the reconciliation still balances.
"""

from __future__ import annotations

from app.services.migration import RunOptions, WriteResult, _write_records
from app.services.transform.types import RecordDraft
from app.services.validation import ValidationOutcome


def drafts(entity: str, n: int, parent: str | None = None) -> list[RecordDraft]:
    id_field = {"organization": "organization_id", "contact": "contact_id"}[entity]
    out = []
    for i in range(n):
        payload = {id_field: f"{entity[0]}{i}"}
        if parent is not None:
            payload["organization_id"] = parent
        out.append(
            RecordDraft(entity=entity, dataset=f"{entity}s.csv", source_row=i + 1, payload=payload)
        )
    return out


def outcome(orgs: int, contacts: int) -> ValidationOutcome:
    return ValidationOutcome(
        valid={
            "organization": drafts("organization", orgs),
            "contact": drafts("contact", contacts, "o0"),
        }
    )


class Target:
    """answers from a script: one outcome per call, the last one repeated."""

    def __init__(self, *outcomes: str) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def write(self, entity: str, payload: dict, record_id: str) -> WriteResult:
        self.calls += 1
        kind = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if kind == "accepted":
            return WriteResult(outcome="accepted", created=True, http_status=201)
        if kind == "rejected":
            return WriteResult(outcome="rejected", http_status=422, error_type="business_rule")
        return WriteResult(
            outcome="failed", attempts=3, error_type="transport_error", message="refused"
        )


def test_failures_in_a_row_open_the_breaker_and_nothing_more_is_sent() -> None:
    target = Target("failed")
    tallies, state = _write_records(target, outcome(25, 7), RunOptions(), breaker_threshold=5)
    org = tallies["organization"]
    assert (org.attempted, org.failed, org.not_attempted) == (5, 5, 20)
    # the contacts were never reached, not blocked: the run stopped before deciding about them
    contact = tallies["contact"]
    assert (contact.attempted, contact.blocked, contact.not_attempted) == (0, 0, 7)
    assert target.calls == 5
    assert state.stopped and "5 records in a row" in state.stopped
    assert len(state.failures) == 5


def test_a_refusal_resets_the_count_because_a_target_that_says_no_is_up() -> None:
    script = ["failed", "failed", "rejected"] * 5 + ["accepted"] * 10
    target = Target(*script)
    tallies, state = _write_records(target, outcome(25, 0), RunOptions(), breaker_threshold=3)
    assert state.stopped is None
    assert tallies["organization"].attempted == 25
    assert tallies["organization"].rejected == 5


def test_the_breaker_threshold_counts_records_not_requests() -> None:
    # each failed record already used three attempts; two records is two, not six
    tallies, state = _write_records(
        Target("failed"), outcome(3, 0), RunOptions(), breaker_threshold=3
    )
    assert tallies["organization"].failed == 3
    assert state.stopped is not None
    tallies, state = _write_records(
        Target("failed"), outcome(2, 0), RunOptions(), breaker_threshold=3
    )
    assert state.stopped is None


def test_the_heartbeat_is_offered_once_per_record() -> None:
    beats = []
    _write_records(
        Target("accepted"),
        outcome(4, 3),
        RunOptions(),
        heartbeat=lambda: beats.append(1),
    )
    assert len(beats) == 7


def test_valid_records_are_all_accounted_for_when_the_breaker_opens_mid_entity() -> None:
    out = outcome(12, 9)
    tallies, _ = _write_records(
        Target("accepted", "accepted", "failed"), out, RunOptions(), breaker_threshold=4
    )
    for entity, records in out.valid.items():
        t = tallies[entity]
        assert t.attempted + t.blocked + t.not_attempted == len(records), entity
