"""the scorer is judged on hand-built proposals so every metric is checked by arithmetic."""

import json
from pathlib import Path

import pytest

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


# --- the variants, the series and the summary eval, none of them calling a model ---------------

from app.ai.provider import LlmError, StructuredResult  # noqa: E402
from app.ai.schemas import ReadinessSummary  # noqa: E402
from app.services.evaluation import (  # noqa: E402
    EvalReport,
    EvalRow,
    opaque_variant,
    run_baseline_eval,
    run_summary_eval,
    summarize_series,
)
from app.services.profiling import load_source  # noqa: E402


@pytest.fixture
def opaque_dir():
    """under generated/, which is a source root: the loader refuses anything outside the roots,
    a temporary directory included."""
    import shutil
    import uuid

    path = Path("generated") / f"test-opaque-{uuid.uuid4().hex[:8]}"
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_the_opaque_variant_renames_every_column_and_keeps_shared_keys_aligned(
    opaque_dir,
) -> None:
    golden = load_golden()
    variant = opaque_variant(golden, out_dir=opaque_dir)
    codes = variant["renamed"]
    assert all(code.startswith("c") and code[1:].isdigit() for code in codes.values())
    # a column several files share keeps one code everywhere
    shared = codes["acct_num"]
    for name in ("organizations.csv", "contacts.csv", "subscriptions.json", "activity.csv"):
        spec = variant["datasets"][name]
        frame = load_source(spec["kind"], spec["location"])
        assert shared in frame.columns
        assert not set(frame.columns) & set(codes)  # no original name survives
        original = golden["datasets"][name]
        before = load_source(original["kind"], original["location"])
        assert len(frame) == len(before)
        assert list(frame.columns) == [codes[c] for c in before.columns]
        # same answers, keyed by the new names
        assert spec["fields"][codes["acct_num"]] == original["fields"]["acct_num"]


def test_the_opaque_csv_keeps_every_value_as_it_was(opaque_dir) -> None:
    golden = load_golden()
    variant = opaque_variant(golden, out_dir=opaque_dir)
    before = load_source("csv", golden["datasets"]["organizations.csv"]["location"])
    after = load_source("csv", variant["datasets"]["organizations.csv"]["location"])
    assert before.to_numpy().tolist() == after.to_numpy().tolist()


def test_the_baseline_scores_the_comparison_on_its_own() -> None:
    report = run_baseline_eval(out_dir=None)
    m = report.metrics
    assert (report.variant, report.provider) == ("baseline", "none")
    assert m["fields"] == 41
    # the comparison refuses the semantic calls, so they land as unresolved, not wrong
    assert m["unresolved"] > m["wrong"]
    unresolved = {r.source_field for r in report.rows if r.outcome == "unresolved"}
    assert "acct_num" in unresolved and "full_name" in unresolved


def _report(outcomes: dict[str, str], accuracy: float) -> EvalReport:
    rows = [
        EvalRow(
            dataset="d",
            source_field=field,
            expected="x",
            accept=[],
            clarify_expected=False,
            clarify_ok=False,
            proposed="x",
            confidence=0.9,
            clarification_required=False,
            transformation_required=False,
            outcome=outcome,  # type: ignore[arg-type]
            reason="r",
        )
        for field, outcome in outcomes.items()
    ]
    return EvalReport(
        ran_at="t",
        provider="p",
        model="m",
        prompt_version="v",
        golden_path="g",
        datasets=["d"],
        metrics={"accuracy": accuracy, "wrong": 0},
        usage={"input_tokens": 10, "output_tokens": 5},
        rows=rows,
    )


def test_a_series_reports_the_spread_and_the_fields_that_moved() -> None:
    series = summarize_series(
        [
            _report({"a": "correct", "b": "correct"}, 1.0),
            _report({"a": "correct", "b": "deferred"}, 0.5),
        ]
    )
    assert series["runs"] == 2
    assert series["metrics"]["accuracy"] == {"min": 0.5, "max": 1.0, "mean": 0.75}
    assert series["unstable_fields"] == {"d:b": ["correct", "deferred"]}
    assert series["usage"] == {"input_tokens": 20, "output_tokens": 10}


CONTENT = json.loads(Path("evals/readiness_report_apex.json").read_text())


class ScriptedSummaries:
    """drafts from a script: a good one, one with an invented number, or no answer."""

    name = "fake"
    model = "fake-writer"

    def __init__(self, *script: str) -> None:
        self.script = list(script)
        self.calls = 0

    def complete(self, *, system, user, output, max_tokens):
        self.calls += 1
        kind = self.script[min(self.calls, len(self.script)) - 1]
        if kind == "down":
            raise LlmError("fake unreachable")
        from app.services.readiness.summary import _explain_codes

        codes = _explain_codes(CONTENT["work_items"])
        figure = "4,187,333" if kind == "invented" else "7,163"
        draft = ReadinessSummary.model_validate(
            {
                "headline": f"{CONTENT['status']}: Apex cannot go live yet",
                "summary": f"The rehearsal accepted {figure} records.",
                "blocker_explanations": [
                    {"code": c, "explanation": "it stops records"} for c in codes
                ],
            }
        )
        return StructuredResult(parsed=draft, provider="fake", model="fake-writer", latency_ms=5.0)


def test_the_summary_eval_counts_first_passes_feedback_passes_and_fallbacks() -> None:
    report = run_summary_eval(
        ScriptedSummaries("good", "invented", "good", "invented", "invented", "invented", "down"),
        CONTENT,
        runs=4,
        out_dir=None,
    )
    m = report.metrics
    assert m["runs"] == 4
    assert m["passed_first_attempt"] == 1
    assert m["passed_after_feedback"] == 1
    assert m["fell_back_to_template"] == 2
    assert m["provider_errors"] == 1
    assert next(iter(m["rejection_kinds"])) == "these numbers do not appear in the facts"
