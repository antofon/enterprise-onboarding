"""the command line against the database: the later stages run from the terminal on a reviewed
project and print the same counts the workbench shows. The commands reach the target through the
app in process, the way the api tests do."""

from __future__ import annotations

import json
from contextlib import nullcontext

import pytest
from typer.testing import CliRunner

from app import cli
from app.core.db import get_session_factory
from tests.golden import ready_project

pytestmark = pytest.mark.integration

SMALL = ("organizations.csv", "contacts.csv")
runner = CliRunner()


@pytest.fixture
def project(client, monkeypatch):
    monkeypatch.setattr(cli, "_http_client", lambda timeout: nullcontext(client))
    session = get_session_factory()()
    found = ready_project(session, datasets=SMALL)
    session.close()
    return found


def run(*args: str) -> str:
    """the command's stdout. log lines go to stderr, so stdout is only the answer."""
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    return result.stdout


def test_check_and_migrate_report_the_schema_revision(client) -> None:
    from app.core.migrations import head_revision

    assert f"schema         {head_revision()}" in run("check")
    assert f"database at schema revision {head_revision()}" in run("migrate")


def test_the_later_stages_run_from_the_terminal(project, tmp_path) -> None:
    plan = run("transformation-plan", "latest")
    assert "Apex Equipment Services, configuration" in plan
    assert "acct_num" in plan and "not migrated" in plan

    validation = run("validate", "latest")
    assert "validation" in validation and "completed" in validation
    assert "organization" in validation and "889" in validation

    rehearsal = run("dry-run", str(project.id), "--limit", "10")
    assert "dry_run" in rehearsal and "completed" in rehearsal
    assert "target namespace" in rehearsal

    reconciliation = run("reconcile", "latest")
    assert "balanced" in reconciliation
    recheck = run("reconcile", "latest", "--recheck")
    assert "(recheck): balanced" in recheck

    out = tmp_path / "readiness.md"
    written = run("readiness-report", "latest", "--out", str(out), "--no-model")
    assert "BLOCKED: report" in written and "(summary: template)" in written
    assert out.read_text().startswith("# ")

    as_json = json.loads(run("readiness-report", "latest", "--format", "json", "--no-model"))
    assert as_json["status"] == "BLOCKED"


def test_a_project_reference_that_is_not_a_uuid_is_refused(client) -> None:
    result = runner.invoke(cli.app, ["validate", "not-a-uuid"])
    assert result.exit_code == 2
    assert "PROJECT is a uuid" in result.output


def test_latest_with_no_projects_says_so(client) -> None:
    result = runner.invoke(cli.app, ["validate", "latest"])
    assert result.exit_code == 1
    assert "no projects yet" in result.output


def test_reconcile_needs_a_dry_run(project) -> None:
    result = runner.invoke(cli.app, ["reconcile", "latest"])
    assert result.exit_code == 1
    assert "no dry run on this project yet" in result.output
