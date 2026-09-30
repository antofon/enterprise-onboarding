"""the scorer is judged on hand-built proposals so every metric is checked by arithmetic."""

import json
from pathlib import Path

from app.ai.schemas import MappingProposal, SuggestedMapping
from app.services.evaluation import _judge, load_golden, score


def _m(name, target, conf=0.9, ask=False, transform=False) -> SuggestedMapping:
    return SuggestedMapping(
        source_field=name,
        target_field=target,
        confidence=conf,
        reason="r",
        transformation_required=transform,
        transformation="rule" if transform else None,
        clarification_required=ask,
        clarification_question="q?" if ask else None,
    )


def test_judge_outcomes() -> None:
    target = {"expected": "organization.name"}
    assert _judge(target, "organization.name", False) == "correct"
    assert _judge(target, "organization.website", False) == "wrong"
    assert _judge(target, None, False) == "unresolved"
    assert _judge(target, None, True) == "deferred"
    assert _judge({**target, "clarify_ok": True}, None, True) == "correct"

    drop = {"expected": None}
    assert _judge(drop, None, False) == "correct"
    assert _judge(drop, None, True) == "correct"
    assert _judge(drop, "organization.tags", False) == "wrong"
    tags_ok = {**drop, "accept": ["organization.tags"]}
    assert _judge(tags_ok, "organization.tags", False) == "correct"

    ask = {"expected": None, "clarify": True, "accept": ["organization.account_priority"]}
    assert _judge(ask, None, True) == "correct"
    assert _judge(ask, "organization.account_priority", True) == "correct"
    assert _judge(ask, "organization.account_priority", False) == "overconfident"
    assert _judge(ask, "subscription.plan", False) == "wrong"
    assert _judge(ask, None, False) == "wrong"


def test_score_counts_by_hand() -> None:
    golden = {
        "datasets": {
            "orgs.csv": {
                "fields": {
                    "a": {"expected": "organization.name"},
                    "b": {"expected": "organization.website", "transformation": True},
                    "c": {"expected": None},
                    "d": {"expected": None, "clarify": True},
                    "e": {"expected": "organization.industry"},
                }
            }
        }
    }
    proposal = MappingProposal(
        dataset="orgs.csv",
        entity="organization",
        mappings=[
            _m("a", "organization.name", 0.95),
            _m("b", "organization.website", 0.9, transform=True),
            _m("c", "organization.tags", 0.9),  # wrong, high confidence
            _m("d", None, 0.4, ask=True),
            # e is missing entirely
        ],
        observations=[],
    )
    rows, m = score({"orgs.csv": proposal}, golden, high_confidence=0.85)
    by = {r.source_field: r.outcome for r in rows}
    assert by == {"a": "correct", "b": "correct", "c": "wrong", "d": "correct", "e": "unresolved"}
    assert m["fields"] == 5 and m["correct"] == 3 and m["accuracy"] == 0.6
    assert m["target_fields"] == 3 and m["target_correct"] == 2
    assert m["target_accuracy"] == round(2 / 3, 3)
    assert m["wrong"] == 1 and m["unresolved"] == 1 and m["deferred"] == 0
    assert m["clarify_expected"] == 1 and m["clarify_flagged"] == 1
    assert m["clarification_recall"] == 1.0 and m["clarification_false_positives"] == 0
    assert m["transformation_flag_recall"] == 1.0
    assert m["incorrect_high_confidence"] == 1
    assert m["mean_confidence_correct"] == round((0.95 + 0.9 + 0.4) / 3, 3)
    assert m["mean_confidence_incorrect"] == 0.9


def test_golden_set_matches_the_sample() -> None:
    """every golden field exists in the sample, every expected target exists in the catalog."""
    from app.services.profiling import load_source
    from app.target.catalog import catalog_by_path

    golden = load_golden(Path("evals/expected_mappings.json"))
    catalog = catalog_by_path()
    for name, spec in golden["datasets"].items():
        columns = set(load_source(spec["kind"], spec["location"]).columns)
        assert set(spec["fields"]) == columns, name
        for field, fspec in spec["fields"].items():
            for target in [fspec.get("expected"), *fspec.get("accept", [])]:
                assert target is None or target in catalog, f"{name}.{field}: {target}"
    # the golden set carries at least one genuinely ambiguous field
    assert any(f.get("clarify") for d in golden["datasets"].values() for f in d["fields"].values())
    json.dumps(golden)  # round-trips
