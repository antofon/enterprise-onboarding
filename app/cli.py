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


if __name__ == "__main__":
    app()
