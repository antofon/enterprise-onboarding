"""backend operations from the terminal. not feature parity with the ui, just the useful bits."""

from __future__ import annotations

import typer

from app import __version__
from app.core.config import get_settings
from app.core.db import check_db
from app.core.logging import configure_logging

app = typer.Typer(
    help="enterprise-onboarding: profile, map, dry-run and report from the terminal.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """enterprise-onboarding command line."""


@app.command()
def check() -> None:
    """show the resolved config and whether postgres answers."""
    configure_logging()
    settings = get_settings()
    db_ok = check_db()
    typer.echo(f"version        {__version__}")
    typer.echo(f"env            {settings.app_env}")
    typer.echo(f"database       {'ok' if db_ok else 'unreachable'}")
    typer.echo(f"llm provider   {settings.effective_llm_provider}")
    typer.echo(f"llm model      {settings.llm_model or '-'}")
    raise typer.Exit(code=0 if db_ok else 1)


@app.command("generate-data")
def generate_data(
    rows: int = typer.Option(10_000, help="organizations to generate; everything else scales"),
    seed: int = typer.Option(42, help="same seed, same bytes"),
    out: str = typer.Option("generated/apex", help="output directory"),
) -> None:
    """write a synthetic customer with realistic defects into OUT."""
    from app.data.synthetic import generate

    summary = generate(rows=rows, seed=seed, out_dir=out)
    for name, count in summary.files.items():
        typer.echo(f"{count:>8}  {name}")
    kinds = len(summary.defects)
    typer.echo(f"{sum(summary.defects.values()):>8}  injected defects across {kinds} kinds")
    typer.echo(f"manifest: {summary.out_dir / 'manifest.json'}")


@app.command()
def profile(
    location: str = typer.Argument(..., help="csv or json under a source root, or a feed url"),
    kind: str | None = typer.Option(None, help="csv | json | api (guessed from the location)"),
    columns: bool = typer.Option(True, help="print the per-column table"),
    compare: bool = typer.Option(False, help="classify every column against the target catalog"),
    entity: str | None = typer.Option(None, help="target entity the file is about (guessed)"),
) -> None:
    """profile one source without the api or the database: types, nulls, uniqueness, issues."""
    from app.models.project import DatasetKind
    from app.services.profiling import assess, load_source, profile_frame

    if kind is None:
        kind = "api" if location.startswith("http") else location.rsplit(".", 1)[-1].lower()
    try:
        dataset_kind = DatasetKind(kind)
    except ValueError as exc:
        raise typer.BadParameter("kind must be csv, json or api") from exc

    df = load_source(dataset_kind, location)
    result = profile_frame(df, name=location.rsplit("/", 1)[-1])
    result.issues = assess(result)

    typer.echo(f"{result.name}: {result.row_count} rows, {result.column_count} columns")
    key = result.key_column or "none found"
    typer.echo(
        f"key column {key}, {result.key_missing} missing, {result.key_duplicates} duplicated, "
        f"{result.exact_duplicate_rows} exact duplicate rows, {result.duration_ms} ms"
    )
    if columns:
        typer.echo("")
        typer.echo(
            f"{'column':<24} {'type':<10} {'null%':>6} {'uniq%':>6} {'distinct':>8}  samples"
        )
        for f in result.fields:
            samples = ", ".join(f.sample_values[:3])
            typer.echo(
                f"{f.name:<24} {f.inferred_type:<10} {f.null_pct:>6} {f.unique_pct:>6} "
                f"{f.distinct_count:>8}  {samples}"
            )
    counts = result.issue_counts()
    typer.echo("")
    typer.echo(
        f"issues: {counts['error']} error, {counts['warning']} warning, {counts['info']} info"
    )
    for issue in result.issues:
        pct = f" ({issue.pct}%)" if issue.pct is not None else ""
        typer.echo(f"  [{issue.severity:<7}] {issue.message}{pct}")

    if compare:
        from app.services.comparison import compare as compare_schemas

        report = compare_schemas([result], entities={result.name: entity} if entity else None)
        guessed = report.datasets[result.name] or "unknown"
        typer.echo("")
        typer.echo(
            f"schema comparison, entity {guessed}: "
            + ", ".join(f"{k} {v}" for k, v in report.counts.items() if v)
        )
        for fc in report.fields:
            target = fc.target or (fc.candidates[0].target + "?" if fc.candidates else "-")
            typer.echo(f"  {fc.source_field:<24} {fc.classification:<24} {target:<34} {fc.reason}")


def _require_provider():
    from app.ai import build_provider

    provider = build_provider()
    if provider.name == "none":
        typer.echo(
            "manual mode: no model provider configured. set LLM_PROVIDER=anthropic|openai and "
            "its key in .env, or map by hand in the workbench.",
            err=True,
        )
        raise typer.Exit(code=1)
    return provider


@app.command()
def suggest(
    location: str = typer.Argument(..., help="csv or json under a source root"),
    kind: str | None = typer.Option(None, help="csv | json (guessed from the location)"),
    entity: str | None = typer.Option(None, help="target entity the file is about (guessed)"),
    rules: str | None = typer.Option(
        "sample_customer/business_rules.md", help="the customer's business rules, or '' for none"
    ),
    customer: str = typer.Option("the customer", help="customer name for the prompt"),
) -> None:
    """ask the configured model for a target per column of one file. no api, no database."""
    from app.ai.prompts import build_mapping_prompt
    from app.models.project import DatasetKind
    from app.services.comparison import compare as compare_schemas
    from app.services.comparison import field_comparison_dict
    from app.services.mapping import propose
    from app.services.profiling import load_source, profile_frame
    from app.services.sources import read_document
    from app.target.catalog import target_field_catalog

    configure_logging()
    provider = _require_provider()
    if kind is None:
        kind = location.rsplit(".", 1)[-1].lower()
    df = load_source(DatasetKind(kind), location)
    profile = profile_frame(df, name=location.rsplit("/", 1)[-1])
    report = compare_schemas([profile], entities={profile.name: entity} if entity else None)
    catalog = target_field_catalog()
    bundle = build_mapping_prompt(
        profile=profile,
        entity=report.datasets[profile.name],
        comparisons=field_comparison_dict(report).get(profile.name, {}),
        catalog=catalog,
        customer=customer,
        rules_text=read_document(rules) if rules else None,
        project_notes=None,
        provider=provider.name,
        model=provider.model,
    )
    typer.echo(f"{provider.name} / {provider.model}, prompt {len(bundle.user):,} chars")
    outcome = propose(provider, bundle, profile=profile, catalog_paths={f.path for f in catalog})
    typer.echo(
        f"{outcome.attempts} attempt(s), {outcome.input_tokens} in / {outcome.output_tokens} out "
        f"tokens, {outcome.latency_ms:.0f} ms"
    )
    typer.echo("")
    typer.echo(f"{'source field':<24} {'target':<34} {'conf':>5}  flags  reason")
    for m in outcome.proposal.mappings:
        flags = ("T" if m.transformation_required else "-") + (
            "?" if m.clarification_required else "-"
        )
        reason = m.reason if len(m.reason) <= 90 else m.reason[:89] + "…"
        target = m.target_field or "-"
        typer.echo(f"{m.source_field:<24} {target:<34} {m.confidence:>5.2f}  {flags:<5}  {reason}")
    questions = [m for m in outcome.proposal.mappings if m.clarification_question]
    if questions:
        typer.echo("")
        typer.echo("questions for the customer:")
        for m in questions:
            typer.echo(f"  [{m.source_field}] {m.clarification_question}")
    if outcome.proposal.observations:
        typer.echo("")
        typer.echo("observations:")
        for o in outcome.proposal.observations:
            typer.echo(f"  - {o}")


@app.command("eval-mapping")
def eval_mapping(
    golden: str = typer.Option("evals/expected_mappings.json", help="the golden set"),
    out: str = typer.Option("evals/results", help="where the result json lands; '' to skip"),
) -> None:
    """run the mapping prompt over the golden set and score it. writes evals/results/*.json."""
    from pathlib import Path

    from app.services.evaluation import run_mapping_eval

    configure_logging()
    provider = _require_provider()
    report = run_mapping_eval(
        provider, golden_path=Path(golden), out_dir=Path(out) if out else None
    )
    m = report.metrics
    typer.echo(f"{report.provider} / {report.model}, prompt {report.prompt_version}")
    typer.echo(
        f"fields {m['fields']}  correct {m['correct']}  accuracy {m['accuracy']}  "
        f"target accuracy {m['target_accuracy']} ({m['target_correct']}/{m['target_fields']})"
    )
    typer.echo(
        f"deferred {m['deferred']}  unresolved {m['unresolved']}  wrong {m['wrong']}  "
        f"overconfident {m['overconfident']}  incorrect at >= {m['high_confidence_threshold']}: "
        f"{m['incorrect_high_confidence']}"
    )
    typer.echo(
        f"clarification recall {m['clarification_recall']} "
        f"({m['clarify_flagged']}/{m['clarify_expected']}), false positives "
        f"{m['clarification_false_positives']}; transformation flag recall "
        f"{m['transformation_flag_recall']}"
    )
    typer.echo(
        f"mean confidence: correct {m['mean_confidence_correct']}, "
        f"incorrect {m['mean_confidence_incorrect']}"
    )
    u = report.usage
    typer.echo(
        f"{u['calls']} calls, {u['attempts']} attempts, {u['input_tokens']} in / "
        f"{u['output_tokens']} out tokens, {u['latency_ms']:.0f} ms"
    )
    typer.echo("")
    for r in report.rows:
        if r.outcome != "correct":
            typer.echo(
                f"  {r.outcome:<13} {r.dataset}.{r.source_field}: proposed "
                f"{r.proposed or '-'} ({r.confidence:.2f}"
                f"{', asks' if r.clarification_required else ''}), expected "
                f"{r.expected or '-'}{' or a question' if r.clarify_expected else ''}"
            )


# --- transformation and migration ---------------------------------------------------------------


def _open_project(project_ref: str):
    """a project by id, or the newest one with `latest`. needs the database."""
    import uuid

    from app.core.db import get_session_factory
    from app.services.projects import get_project, list_projects

    session = get_session_factory()()
    if project_ref == "latest":
        projects = list_projects(session)
        if not projects:
            typer.echo("no projects yet; create one in the workbench or with the api", err=True)
            raise typer.Exit(code=1)
        return session, projects[0]
    try:
        project_id = uuid.UUID(project_ref)
    except ValueError as exc:
        raise typer.BadParameter("PROJECT is a uuid, or `latest`") from exc
    return session, get_project(session, project_id)


def _print_run(run) -> None:
    typer.echo(
        f"{run.kind.value} {run.id} {run.status.value}, {(run.duration_ms or 0) / 1000:.1f}s, "
        f"configuration {run.config_version} as of {run.as_of}"
    )
    columns = ["source_rows", "built", "skipped_by_rule", "valid", "invalid", "with_warnings"]
    if run.kind.value == "dry_run":
        columns += ["attempted", "accepted", "rejected", "failed", "blocked", "retries"]
    header = f"{'entity':<14}" + "".join(f"{c.replace('_', ' '):>17}" for c in columns)
    typer.echo(header)
    from app.models.target import WRITE_ORDER

    entities = [e for e in WRITE_ORDER if e in run.stats]
    for entity in entities:
        stats = run.stats[entity]
        typer.echo(f"{entity:<14}" + "".join(f"{stats.get(c, 0):>17,}" for c in columns))
    if run.issue_counts:
        typer.echo("")
        typer.echo("what validation found:")
        for error_type, count in list(run.issue_counts.items())[:15]:
            typer.echo(f"  {count:>6}  {error_type}")
    if run.applied_rules:
        typer.echo("")
        typer.echo("customer rules applied:")
        for rule, count in run.applied_rules.items():
            typer.echo(f"  {count:>6}  {rule}")
    if run.kind.value == "dry_run":
        typer.echo("")
        counts = ", ".join(f"{k} {v:,}" for k, v in (run.target_counts or {}).items())
        typer.echo(f"target namespace {run.namespace}: {counts or 'purged'}")


@app.command("transformation-plan")
def transformation_plan(
    project: str = typer.Argument("latest", help="project id, or `latest`"),
) -> None:
    """what the approved mappings will do to every column, before any row runs."""
    from app.services.transform import build_plan

    configure_logging()
    session, found = _open_project(project)
    plan = build_plan(session, found)
    typer.echo(
        f"{plan.customer}, configuration {plan.config_version}, dates measured from "
        f"{plan.as_of}; entities {', '.join(plan.entities)}"
    )
    typer.echo(f"record rules: {', '.join(plan.record_rules + plan.cross_dataset_rules)}")
    for dataset in plan.datasets:
        typer.echo("")
        typer.echo(f"{dataset.dataset}  emits {', '.join(dataset.emits) or 'nothing'}")
        for rule in dataset.field_rules:
            chain = " > ".join(rule.converters)
            typer.echo(f"  {rule.source_field:<24} -> {', '.join(rule.emits):<40} {chain}")
        for rule in dataset.context_rules:
            typer.echo(f"  {rule.source_field:<24} -> {rule.target_path:<40} (context only)")
        for dropped in dataset.dropped:
            typer.echo(f"  {dropped.source_field:<24}    not migrated: {dropped.why}")
    session.close()


@app.command()
def validate(
    project: str = typer.Argument("latest", help="project id, or `latest`"),
    limit: int | None = typer.Option(None, help="records per entity, for a quick look"),
) -> None:
    """transform and check every record, write nothing. prints what validation found."""
    import httpx

    from app.services.migration import RunOptions, run_validation

    configure_logging()
    session, found = _open_project(project)
    with httpx.Client(timeout=30) as http:
        run = run_validation(
            session, found, http_client=http, options=RunOptions(limit_per_entity=limit)
        )
    _print_run(run)
    session.close()


@app.command("dry-run")
def dry_run(
    project: str = typer.Argument("latest", help="project id, or `latest`"),
    limit: int | None = typer.Option(None, help="records per entity, for a quick look"),
    purge: bool = typer.Option(False, help="throw the staging namespace away afterwards"),
    stop_after: int | None = typer.Option(None, help="give up on an entity after N refusals"),
) -> None:
    """rehearse the migration: every valid record through the target api, in its own namespace."""
    import httpx

    from app.services.migration import RunOptions, run_dry_run

    configure_logging()
    settings = get_settings()
    session, found = _open_project(project)
    typer.echo(f"target {settings.target_api_base_url}")
    with httpx.Client(timeout=settings.target_request_timeout_seconds) as http:
        run = run_dry_run(
            session,
            found,
            http_client=http,
            options=RunOptions(
                limit_per_entity=limit, purge_namespace_after=purge, stop_after_failures=stop_after
            ),
        )
    _print_run(run)
    session.close()


def _print_reconciliation(result) -> None:
    from app.models.target import WRITE_ORDER

    passed = sum(1 for c in result.checks if c["ok"])
    typer.echo(
        f"reconciliation {result.id} ({result.trigger}): {result.status.value}, "
        f"{passed} of {len(result.checks)} checks passed"
    )
    columns = [
        "source_rows",
        "distinct_ids",
        "skipped_by_rule",
        "excluded",
        "valid",
        "not_attempted",
        "blocked",
        "attempted",
        "accepted",
        "in_target",
    ]
    typer.echo(f"{'entity':<14}" + "".join(f"{c.replace('_', ' '):>15}" for c in columns))
    for entity in [e for e in WRITE_ORDER if e in result.entities]:
        row = result.entities[entity]
        typer.echo(f"{entity:<14}" + "".join(f"{int(row.get(c) or 0):>15,}" for c in columns))
    for entity in [e for e in WRITE_ORDER if e in result.entities]:
        reasons = result.entities[entity]["excluded_by_reason"]
        if reasons:
            listed = ", ".join(f"{k} {v:,}" for k, v in reasons.items())
            typer.echo(f"  {entity} excluded: {listed}")
    for d in result.discrepancies:
        sample = ", ".join(d["sample_ids"][:5])
        typer.echo(f"  DISCREPANCY {d['entity']} {d['kind']} ({d['count']:,}): {d['explanation']}")
        if sample:
            typer.echo(f"    sample: {sample}")


@app.command()
def reconcile(
    project: str = typer.Argument("latest", help="project id, or `latest`"),
    run: str | None = typer.Option(None, help="dry run id; default the newest dry run"),
    recheck: bool = typer.Option(False, help="read the target again instead of showing the last"),
) -> None:
    """source against target for a dry run: every count accounted for, every id compared."""
    import uuid

    import httpx

    from app.models.migration import RunKind
    from app.services import migration as migration_service
    from app.services import reconciliation as reconciliation_service

    configure_logging()
    settings = get_settings()
    session, found = _open_project(project)
    if run:
        chosen = migration_service.get_run(session, found, uuid.UUID(run))
    else:
        chosen = migration_service.latest_run(session, found, kind=RunKind.dry_run)
        if chosen is None:
            typer.echo("no dry run on this project yet; run `dry-run` first", err=True)
            raise typer.Exit(code=1)
    if recheck:
        assert chosen.namespace is not None
        with httpx.Client(timeout=settings.target_request_timeout_seconds) as http:
            target = migration_service.TargetClient(
                http,
                settings=settings,
                namespace=chosen.namespace,
                request_prefix=f"recheck-{str(chosen.id)[:8]}",
            )
            result = reconciliation_service.recheck(session, chosen, target=target)
    else:
        result = reconciliation_service.latest_for_run(session, chosen)
        if result is None:
            typer.echo(
                "this run has no reconciliation; it predates reconciliation or is not a dry run",
                err=True,
            )
            raise typer.Exit(code=1)
    _print_reconciliation(result)
    session.close()


@app.command("readiness-report")
def readiness_report(
    project: str = typer.Argument("latest", help="project id, or `latest`"),
    fmt: str = typer.Option("markdown", "--format", help="markdown or json"),
    out: str | None = typer.Option(None, help="write the report to this file instead of stdout"),
    model: bool = typer.Option(
        True, help="let the configured model draft the summary (checked against the facts)"
    ),
) -> None:
    """generate and store a readiness report, and print or save it."""
    import json
    from pathlib import Path

    from app.ai.provider import build_provider
    from app.services.readiness import generate_report

    if fmt not in ("markdown", "json"):
        raise typer.BadParameter("--format is markdown or json")
    configure_logging()
    session, found = _open_project(project)
    report = generate_report(
        session, found, provider=build_provider(), use_model=model, generated_by="cli"
    )
    text = (
        report.markdown
        if fmt == "markdown"
        else json.dumps(report.content, indent=2, ensure_ascii=False, default=str)
    )
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        typer.echo(
            f"{report.status.value}: report {report.id} written to {out} "
            f"(summary: {report.summary_origin})"
        )
    else:
        typer.echo(text)
    session.close()


if __name__ == "__main__":
    app()
