"""the command line without a database: generating a customer, profiling a file, comparing it,
and the commands that need a model saying so when there is none."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from app import cli

runner = CliRunner()


def test_generate_data_writes_a_customer_and_its_defect_manifest(tmp_path) -> None:
    result = runner.invoke(cli.app, ["generate-data", "--rows", "40", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "organizations.csv" in result.output
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["files"]["organizations.csv"] >= 40
    assert "injected defects across" in result.output


def test_profile_and_compare_one_file_without_the_api() -> None:
    result = runner.invoke(
        cli.app, ["profile", "sample_customer/data/organizations.csv", "--compare"]
    )
    assert result.exit_code == 0, result.output
    assert "organizations.csv: 1030 rows, 15 columns" in result.output
    assert "key column acct_num" in result.output
    assert "schema comparison, entity organization" in result.output
    assert "AMBIGUOUS" in result.output


def test_profile_refuses_a_kind_it_cannot_read() -> None:
    result = runner.invoke(cli.app, ["profile", "sample_customer/data/organizations.xml"])
    assert result.exit_code == 2
    assert "kind must be csv, json or api" in result.output


def test_commands_that_need_a_model_say_so_in_manual_mode() -> None:
    for args in (["suggest", "sample_customer/data/organizations.csv"], ["eval-mapping"]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 1
        assert "manual mode" in result.output


def test_check_fails_when_the_database_does_not_answer(monkeypatch) -> None:
    monkeypatch.setattr(cli, "check_db", lambda: False)
    result = runner.invoke(cli.app, ["check"])
    assert result.exit_code == 1
    assert "database       unreachable" in result.output


def test_a_report_format_other_than_markdown_or_json_is_refused() -> None:
    result = runner.invoke(cli.app, ["readiness-report", "latest", "--format", "pdf"])
    assert result.exit_code == 2
    assert "markdown or json" in result.output


def test_the_baseline_eval_runs_with_no_model() -> None:
    result = runner.invoke(cli.app, ["eval-mapping", "--variant", "baseline", "--out", ""])
    assert result.exit_code == 0, result.output
    assert "none / deterministic comparison" in result.stdout
    assert "fields 41  correct 31" in result.stdout


def test_an_unknown_eval_variant_is_refused() -> None:
    result = runner.invoke(cli.app, ["eval-mapping", "--variant", "upside-down"])
    assert result.exit_code == 2
    assert "standard, opaque or baseline" in result.output


def test_repeated_mapping_runs_report_the_spread(monkeypatch, tmp_path) -> None:
    from tests.fakes import FakeProvider

    monkeypatch.setattr(cli, "_require_provider", lambda: FakeProvider())
    result = runner.invoke(cli.app, ["eval-mapping", "--runs", "2", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "--- run 2 of 2" in result.stdout
    assert "fields whose outcome changed between runs: 0" in result.stdout
    series = list(tmp_path.glob("*-standard-series.json"))
    assert len(series) == 1
    assert json.loads(series[0].read_text())["runs"] == 2
    assert len(list(tmp_path.glob("*.json"))) == 3  # two runs and the series


def test_the_summary_eval_prints_its_counts(monkeypatch, tmp_path) -> None:
    from tests.unit.test_evaluation import ScriptedSummaries

    monkeypatch.setattr(cli, "_require_provider", lambda: ScriptedSummaries("good"))
    result = runner.invoke(cli.app, ["eval-summary", "--runs", "2", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "2 runs: 2 passed first time, 0 after feedback, 0 fell back" in result.stdout
    assert len(list(tmp_path.glob("*-summary.json"))) == 1
