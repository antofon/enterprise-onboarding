"""generate the synthetic customer's source files.

    uv run python scripts/generate_customer_data.py --rows 10000 --seed 42 --out generated/apex

same thing as `enterprise-onboarding generate-data`.
"""

from __future__ import annotations

import typer

from app.data.synthetic import generate


def main(
    rows: int = typer.Option(
        10_000, help="number of organizations; contacts, subscriptions and activity scale from it"
    ),
    seed: int = typer.Option(42, help="same seed, same bytes"),
    out: str = typer.Option("generated/apex", help="output directory"),
) -> None:
    summary = generate(rows=rows, seed=seed, out_dir=out)
    for name, count in summary.files.items():
        typer.echo(f"{count:>8}  {name}")
    typer.echo(
        f"{sum(summary.defects.values()):>8}  injected defects across {len(summary.defects)} kinds"
    )
    typer.echo(f"manifest: {summary.out_dir / 'manifest.json'}")


if __name__ == "__main__":
    typer.run(main)
