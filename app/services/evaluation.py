"""measure the model's two jobs against known answers. no database: profiles come straight from
the files, proposals straight from the provider, and every result is a json file under
evals/results that the docs quote from. every number here is counted.

  mapping       the golden set (`evals/expected_mappings.json`), scored field by field. Three
                ways to run it: `standard` is the committed sample as it is; `opaque` is the
                same rows with every column renamed to a code that carries no meaning, which
                measures how much rests on the names and whether answers without them stay
                honest about their confidence; `baseline` is the deterministic comparison alone,
                with no model, which is what the model is measured against.
  summary       the readiness summary drafted over and over from one report's facts, each draft
                put through the same check the report applies: how often it passes first time,
                how often only after feedback, how often the code-written summary takes over.
"""

from __future__ import annotations

import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from app.ai.prompts import PROMPT_VERSION, build_mapping_prompt
from app.ai.provider import LlmProvider
from app.ai.schemas import MappingProposal
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.comparison import compare, field_comparison_dict
from app.services.mapping import ProposalOutcome, drafts_from_comparison, propose
from app.services.profiling import load_source, profile_frame
from app.services.profiling.types import DatasetProfile
from app.services.sources import read_document
from app.target.catalog import target_field_catalog

log = get_logger(__name__)

GOLDEN_PATH = Path("evals/expected_mappings.json")
RESULTS_DIR = Path("evals/results")
OPAQUE_DIR = Path("generated/eval-opaque")

Variant = Literal["standard", "opaque", "baseline"]

Outcome = Literal["correct", "deferred", "unresolved", "wrong", "overconfident"]


class EvalRow(BaseModel):
    dataset: str
    source_field: str
    expected: str | None
    accept: list[str | None]
    clarify_expected: bool
    clarify_ok: bool
    proposed: str | None
    confidence: float
    clarification_required: bool
    transformation_required: bool
    outcome: Outcome
    reason: str


class EvalReport(BaseModel):
    ran_at: str
    provider: str
    model: str
    prompt_version: str
    variant: Variant = "standard"
    golden_path: str
    datasets: list[str]
    metrics: dict[str, Any]
    usage: dict[str, Any]
    observations: dict[str, list[str]] = Field(default_factory=dict)
    rows: list[EvalRow]


def load_golden(path: Path = GOLDEN_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def _judge(spec: dict[str, Any], proposed: str | None, asks: bool) -> Outcome:
    expected = spec.get("expected")
    accept = set(spec.get("accept", []))
    clarify = bool(spec.get("clarify"))
    clarify_ok = bool(spec.get("clarify_ok"))
    hit = proposed == expected or proposed in accept

    if clarify:
        # the right answer is a question; a target from `accept` may ride along with it
        if asks and (proposed is None or proposed in accept):
            return "correct"
        if proposed in accept:
            return "overconfident"
        return "wrong"
    if expected is None:
        if proposed is None or proposed in accept:
            return "correct"
        return "wrong"
    # a real target was expected
    if hit:
        return "correct"
    if proposed is None:
        if asks and clarify_ok:
            return "correct"
        return "deferred" if asks else "unresolved"
    return "wrong"


def score(
    proposals: dict[str, MappingProposal],
    golden: dict[str, Any],
    *,
    high_confidence: float,
) -> tuple[list[EvalRow], dict[str, Any]]:
    rows: list[EvalRow] = []
    for dataset, spec in golden["datasets"].items():
        proposal = proposals.get(dataset)
        by_field = {m.source_field: m for m in proposal.mappings} if proposal else {}
        for field_name, fspec in spec["fields"].items():
            m = by_field.get(field_name)
            if m is None:
                rows.append(
                    EvalRow(
                        dataset=dataset,
                        source_field=field_name,
                        expected=fspec.get("expected"),
                        accept=fspec.get("accept", []),
                        clarify_expected=bool(fspec.get("clarify")),
                        clarify_ok=bool(fspec.get("clarify_ok")),
                        proposed=None,
                        confidence=0.0,
                        clarification_required=False,
                        transformation_required=False,
                        outcome="unresolved",
                        reason="no proposal for this field",
                    )
                )
                continue
            rows.append(
                EvalRow(
                    dataset=dataset,
                    source_field=field_name,
                    expected=fspec.get("expected"),
                    accept=fspec.get("accept", []),
                    clarify_expected=bool(fspec.get("clarify")),
                    clarify_ok=bool(fspec.get("clarify_ok")),
                    proposed=m.target_field,
                    confidence=round(float(m.confidence), 3),
                    clarification_required=m.clarification_required,
                    transformation_required=m.transformation_required,
                    outcome=_judge(fspec, m.target_field, m.clarification_required),
                    reason=m.reason,
                )
            )

    total = len(rows)
    correct = [r for r in rows if r.outcome == "correct"]
    incorrect = [r for r in rows if r.outcome in ("wrong", "overconfident")]
    with_target = [r for r in rows if r.expected is not None]
    target_correct = [r for r in with_target if r.outcome == "correct"]
    clarify_expected = [r for r in rows if r.clarify_expected]
    clarify_hit = [r for r in clarify_expected if r.clarification_required]
    clarify_fp = [
        r for r in rows if r.clarification_required and not r.clarify_expected and not r.clarify_ok
    ]
    transform_expected = [
        r
        for r in rows
        if golden["datasets"][r.dataset]["fields"][r.source_field].get("transformation")
        and r.outcome == "correct"
        and r.proposed is not None
    ]
    transform_hit = [r for r in transform_expected if r.transformation_required]

    def mean(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 3) if xs else None

    metrics: dict[str, Any] = {
        "fields": total,
        "correct": len(correct),
        "accuracy": round(len(correct) / total, 3) if total else None,
        "target_fields": len(with_target),
        "target_correct": len(target_correct),
        "target_accuracy": round(len(target_correct) / len(with_target), 3)
        if with_target
        else None,
        "deferred": sum(1 for r in rows if r.outcome == "deferred"),
        "unresolved": sum(1 for r in rows if r.outcome == "unresolved"),
        "wrong": sum(1 for r in rows if r.outcome == "wrong"),
        "overconfident": sum(1 for r in rows if r.outcome == "overconfident"),
        "clarify_expected": len(clarify_expected),
        "clarify_flagged": len(clarify_hit),
        "clarification_recall": round(len(clarify_hit) / len(clarify_expected), 3)
        if clarify_expected
        else None,
        "clarification_false_positives": len(clarify_fp),
        "transformation_flag_recall": round(len(transform_hit) / len(transform_expected), 3)
        if transform_expected
        else None,
        "incorrect_high_confidence": sum(1 for r in incorrect if r.confidence >= high_confidence),
        "high_confidence_threshold": high_confidence,
        "mean_confidence_correct": mean([r.confidence for r in correct]),
        "mean_confidence_incorrect": mean([r.confidence for r in incorrect]),
    }
    return rows, metrics


def _profiles(golden: dict[str, Any], settings: Settings) -> list[DatasetProfile]:
    return [
        profile_frame(load_source(spec["kind"], spec["location"], settings=settings), name=name)
        for name, spec in golden["datasets"].items()
    ]


def opaque_variant(golden: dict[str, Any], out_dir: Path = OPAQUE_DIR) -> dict[str, Any]:
    """the golden set again with every column renamed to a code that carries no meaning (c01,
    c02, ...). Same rows, same values, same file names, same rules document. A column that
    appears in several files keeps one code everywhere, so the shared keys still line up the way
    they do in a real export. The rules document still names some columns by their old names,
    which is what happens when a customer's documentation is older than its export."""
    codes: dict[str, str] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    variant = {**golden, "datasets": {}}
    for name, spec in golden["datasets"].items():
        frame = load_source(spec["kind"], spec["location"])
        for column in frame.columns:
            codes.setdefault(column, f"c{len(codes) + 1:02d}")
        renamed = frame.rename(columns=codes)
        target = out_dir / Path(spec["location"]).name
        if spec["kind"] == "csv":
            renamed.to_csv(target, index=False)
        else:
            records = [
                {k: v for k, v in row.items() if not (isinstance(v, float) and pd.isna(v))}
                for row in renamed.to_dict(orient="records")
            ]
            target.write_text(json.dumps({"data": records}, ensure_ascii=False))
        variant["datasets"][name] = {
            **spec,
            "location": str(target),
            "fields": {codes[field]: info for field, info in spec["fields"].items()},
        }
    variant["renamed"] = codes
    return variant


def run_mapping_eval(
    provider: LlmProvider,
    *,
    golden_path: Path = GOLDEN_PATH,
    golden: dict[str, Any] | None = None,
    variant: Variant = "standard",
    out_dir: Path | None = RESULTS_DIR,
    settings: Settings | None = None,
) -> EvalReport:
    settings = settings or get_settings()
    golden = golden or load_golden(golden_path)
    if variant == "opaque" and "renamed" not in golden:
        golden = opaque_variant(golden)
    rules_text = (
        read_document(golden["rules_document"], settings) if golden.get("rules_document") else None
    )
    profiles = _profiles(golden, settings)
    report = compare(profiles)
    by_dataset = field_comparison_dict(report)
    catalog = target_field_catalog()
    catalog_paths = {f.path for f in catalog}

    proposals: dict[str, MappingProposal] = {}
    observations: dict[str, list[str]] = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "latency_ms": 0.0, "attempts": 0, "calls": 0}

    def ask(profile: DatasetProfile) -> ProposalOutcome:
        bundle = build_mapping_prompt(
            profile=profile,
            entity=report.datasets.get(profile.name),
            comparisons=by_dataset.get(profile.name, {}),
            catalog=catalog,
            customer=golden.get("customer", "the customer"),
            rules_text=rules_text,
            project_notes=None,
            provider=provider.name,
            model=provider.model,
        )
        return propose(
            provider, bundle, profile=profile, catalog_paths=catalog_paths, settings=settings
        )

    workers = max(1, min(settings.llm_parallel_calls, len(profiles)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(ask, profiles))
    for profile, outcome in zip(profiles, outcomes, strict=True):
        proposals[profile.name] = outcome.proposal
        observations[profile.name] = list(outcome.proposal.observations)
        usage["input_tokens"] += outcome.input_tokens
        usage["output_tokens"] += outcome.output_tokens
        usage["latency_ms"] = round(usage["latency_ms"] + outcome.latency_ms, 1)
        usage["attempts"] += outcome.attempts
        usage["calls"] += 1
        log.info(
            "eval_dataset_proposed",
            dataset=profile.name,
            attempts=outcome.attempts,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            duration_ms=outcome.latency_ms,
        )

    rows, metrics = score(proposals, golden, high_confidence=settings.mapping_high_confidence)
    result = EvalReport(
        ran_at=datetime.now(UTC).isoformat(timespec="seconds"),
        provider=provider.name,
        model=provider.model,
        prompt_version=PROMPT_VERSION,
        variant=variant,
        golden_path=str(golden_path),
        datasets=list(golden["datasets"]),
        metrics=metrics,
        usage=usage,
        observations=observations,
        rows=rows,
    )
    if out_dir is not None:
        path = _write(result, out_dir, f"{provider.name}-{provider.model}", variant)
        log.info(
            "eval_written",
            path=str(path),
            **{
                k: v
                for k, v in metrics.items()
                if k
                in (
                    "accuracy",
                    "target_accuracy",
                    "clarification_recall",
                    "incorrect_high_confidence",
                )
            },
        )
    return result


def _write(report: BaseModel, out_dir: Path, who: str, variant: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    suffix = "" if variant == "standard" else f"-{variant}"
    path = out_dir / f"{stamp}-{who.replace('/', '_')}{suffix}.json"
    path.write_text(report.model_dump_json(indent=2) + "\n")
    return path


# --- the baseline: what the comparison gets right with no model at all --------------------------


def run_baseline_eval(
    *,
    golden_path: Path = GOLDEN_PATH,
    golden: dict[str, Any] | None = None,
    out_dir: Path | None = RESULTS_DIR,
    settings: Settings | None = None,
) -> EvalReport:
    """manual mode scored like a model: MATCHED and TRANSFORMATION_REQUIRED fields propose the
    comparison's target, AMBIGUOUS fields ask, the rest wait for a person. No confidence exists,
    so it is recorded as 0 and the confidence metrics mean nothing for this row."""
    settings = settings or get_settings()
    golden = golden or load_golden(golden_path)
    profiles = _profiles(golden, settings)
    report = compare(profiles)
    by_dataset = field_comparison_dict(report)
    proposals: dict[str, MappingProposal] = {}
    for profile in profiles:
        drafts = drafts_from_comparison(profile, by_dataset.get(profile.name, {}))
        proposals[profile.name] = MappingProposal.model_validate(
            {
                "dataset": profile.name,
                "entity": report.datasets.get(profile.name),
                "mappings": [
                    {
                        "source_field": d.source_field,
                        "target_field": d.target,
                        "confidence": 0.0,
                        "reason": d.reason,
                        "transformation_required": d.transformation_required,
                        "transformation": d.transformation,
                        "clarification_required": d.clarification_required,
                        "clarification_question": d.question,
                    }
                    for d in drafts
                ],
                "observations": [],
            }
        )
    rows, metrics = score(proposals, golden, high_confidence=settings.mapping_high_confidence)
    result = EvalReport(
        ran_at=datetime.now(UTC).isoformat(timespec="seconds"),
        provider="none",
        model="deterministic comparison",
        prompt_version="-",
        variant="baseline",
        golden_path=str(golden_path),
        datasets=list(golden["datasets"]),
        metrics=metrics,
        usage={"calls": 0, "attempts": 0, "input_tokens": 0, "output_tokens": 0},
        rows=rows,
    )
    if out_dir is not None:
        _write(result, out_dir, "none-comparison", "baseline")
    return result


# --- repeated runs ----------------------------------------------------------------------------

SERIES_METRICS = (
    "correct",
    "accuracy",
    "target_correct",
    "wrong",
    "overconfident",
    "deferred",
    "unresolved",
    "incorrect_high_confidence",
    "clarification_recall",
    "clarification_false_positives",
    "mean_confidence_correct",
    "mean_confidence_incorrect",
)


def summarize_series(reports: list[EvalReport]) -> dict[str, Any]:
    """the same eval run several times: each metric's min, max and mean, and the fields whose
    outcome was not the same in every run, which is where the model is unstable."""
    out: dict[str, Any] = {"runs": len(reports), "metrics": {}}
    for key in SERIES_METRICS:
        values: list[float] = [
            float(r.metrics[key]) for r in reports if r.metrics.get(key) is not None
        ]
        if values:
            out["metrics"][key] = {
                "min": min(values),
                "max": max(values),
                "mean": round(statistics.fmean(values), 3),
            }
    outcomes: dict[str, set[str]] = {}
    for r in reports:
        for row in r.rows:
            outcomes.setdefault(f"{row.dataset}:{row.source_field}", set()).add(row.outcome)
    out["unstable_fields"] = {k: sorted(v) for k, v in outcomes.items() if len(v) > 1}
    out["usage"] = {
        "input_tokens": sum(r.usage.get("input_tokens", 0) for r in reports),
        "output_tokens": sum(r.usage.get("output_tokens", 0) for r in reports),
    }
    return out


# --- the readiness summary ----------------------------------------------------------------------


class SummaryEvalRun(BaseModel):
    passed: bool
    attempts: int
    rejections: list[list[str]]
    error: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: float


class SummaryEvalReport(BaseModel):
    ran_at: str
    provider: str
    model: str
    prompt_version: str
    source: str
    status: str
    explain_codes: list[str]
    metrics: dict[str, Any]
    runs: list[SummaryEvalRun]


def run_summary_eval(
    provider: LlmProvider,
    content: dict[str, Any],
    *,
    runs: int = 5,
    source: str = "",
    out_dir: Path | None = RESULTS_DIR,
    settings: Settings | None = None,
) -> SummaryEvalReport:
    """draft the summary `runs` times from one report's facts, no cache, each draft through the
    same ask-check-feedback loop the report uses."""
    from app.ai.report_prompt import REPORT_PROMPT_VERSION, build_summary_prompt
    from app.services.readiness.summary import _explain_codes, request_summary, summary_facts

    settings = settings or get_settings()
    facts = summary_facts(content)
    codes = _explain_codes(content["work_items"])
    bundle = build_summary_prompt(
        facts, explain=codes, provider=provider.name, model=provider.model
    )
    results: list[SummaryEvalRun] = []
    for n in range(runs):
        asked = request_summary(
            provider, bundle, facts=facts, status=content["status"], codes=codes, settings=settings
        )
        results.append(
            SummaryEvalRun(
                passed=asked.summary is not None,
                attempts=asked.attempts,
                rejections=asked.rejections,
                error=asked.error,
                input_tokens=asked.input_tokens,
                output_tokens=asked.output_tokens,
                latency_ms=asked.latency_ms,
            )
        )
        log.info(
            "eval_summary_run", run=n + 1, passed=asked.summary is not None, attempts=asked.attempts
        )
    problem_kinds: dict[str, int] = {}
    for run in results:
        for rejection in run.rejections:
            for problem in rejection:
                kind = problem.split(":", 1)[0][:80]
                problem_kinds[kind] = problem_kinds.get(kind, 0) + 1
    metrics = {
        "runs": runs,
        "passed_first_attempt": sum(1 for r in results if r.passed and r.attempts == 1),
        "passed_after_feedback": sum(1 for r in results if r.passed and r.attempts > 1),
        "fell_back_to_template": sum(1 for r in results if not r.passed),
        "provider_errors": sum(1 for r in results if r.error),
        "attempts_total": sum(r.attempts for r in results),
        "rejected_drafts": sum(len(r.rejections) for r in results),
        "rejection_kinds": dict(sorted(problem_kinds.items(), key=lambda kv: -kv[1])),
        "mean_latency_ms": round(statistics.fmean(r.latency_ms for r in results), 1)
        if results
        else None,
        "input_tokens": sum(r.input_tokens for r in results),
        "output_tokens": sum(r.output_tokens for r in results),
    }
    report = SummaryEvalReport(
        ran_at=datetime.now(UTC).isoformat(timespec="seconds"),
        provider=provider.name,
        model=provider.model,
        prompt_version=REPORT_PROMPT_VERSION,
        source=source,
        status=content["status"],
        explain_codes=codes,
        metrics=metrics,
        runs=results,
    )
    if out_dir is not None:
        _write(report, out_dir, f"{provider.name}-{provider.model}", "summary")
    return report
