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


if __name__ == "__main__":
    app()
