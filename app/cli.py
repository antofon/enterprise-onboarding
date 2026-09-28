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


if __name__ == "__main__":
    app()
