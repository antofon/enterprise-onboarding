"""measure the mapping step against a golden set. no database: profiles come straight from the
files, proposals straight from the provider, and the result is a json file under
evals/results that the docs quote from. every number here is counted."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.ai.prompts import PROMPT_VERSION, build_mapping_prompt
from app.ai.provider import LlmProvider
from app.ai.schemas import MappingProposal
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.comparison import compare, field_comparison_dict
from app.services.mapping import ProposalOutcome, propose
from app.services.profiling import load_source, profile_frame
from app.services.profiling.types import DatasetProfile
from app.services.sources import read_document
from app.target.catalog import target_field_catalog

log = get_logger(__name__)

GOLDEN_PATH = Path("evals/expected_mappings.json")
RESULTS_DIR = Path("evals/results")

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


def run_mapping_eval(
    provider: LlmProvider,
    *,
    golden_path: Path = GOLDEN_PATH,
    out_dir: Path | None = RESULTS_DIR,
    settings: Settings | None = None,
) -> EvalReport:
    settings = settings or get_settings()
    golden = load_golden(golden_path)
    rules_text = (
        read_document(golden["rules_document"], settings) if golden.get("rules_document") else None
    )
    profiles = [
        profile_frame(load_source(spec["kind"], spec["location"], settings=settings), name=name)
        for name, spec in golden["datasets"].items()
    ]
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
        golden_path=str(golden_path),
        datasets=list(golden["datasets"]),
        metrics=metrics,
        usage=usage,
        observations=observations,
        rows=rows,
    )
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
        safe_model = provider.model.replace("/", "_")
        path = out_dir / f"{stamp}-{provider.name}-{safe_model}.json"
        path.write_text(result.model_dump_json(indent=2) + "\n")
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
