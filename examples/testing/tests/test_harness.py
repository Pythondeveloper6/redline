"""The full-stack harness must refuse accidental targets and fail closed on missing evidence."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "run.py"


@pytest.fixture
def harness():
    spec = importlib.util.spec_from_file_location("compose_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_requires_disposable_consent(harness, monkeypatch, tmp_path):
    monkeypatch.delenv("REDLINE_COMPOSE_TESTS", raising=False)
    monkeypatch.setenv("REDLINE_TEST_RESULTS", str(tmp_path / "results"))
    assert harness.main() == 2
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize(
    "dsn",
    [
        "mssql+pyodbc://sa:fixture@production/redline_test_integration",
        "mssql+pyodbc://sa:fixture@mssql/customer_database",
    ],
)
def test_provisioning_refuses_external_or_non_disposable_database(harness, monkeypatch, dsn):
    import sqlalchemy

    monkeypatch.setenv("REDLINE_COMPOSE_TESTS", "1")
    monkeypatch.setenv("REDLINE_TEST_MSSQL_DSN", dsn)

    def unexpected_connect(*args, **kwargs):
        pytest.fail("Refused target must not create an engine")

    monkeypatch.setattr(sqlalchemy, "create_engine", unexpected_connect)
    with pytest.raises(RuntimeError, match="Refusing"):
        harness.prepare_sql_server()


def test_missing_dependency_replaces_previous_green_summary(harness, monkeypatch, tmp_path):
    monkeypatch.setenv("REDLINE_COMPOSE_TESTS", "1")
    monkeypatch.setenv("REDLINE_TEST_RESULTS", str(tmp_path))
    (tmp_path / "summary.json").write_text('{"qualified": true}')

    def missing(name):
        raise harness.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(harness.importlib.metadata, "version", missing)
    assert harness.main() == 1
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["qualified"] is False
    assert summary["steps"][0]["error_type"] == "PackageNotFoundError"
    assert (tmp_path / summary["run_id"] / "summary.json").is_file()


@pytest.mark.parametrize(
    "problem", [None, "skipped", "errors", "missing_report", "process_failure"]
)
def test_green_requires_all_processes_and_complete_junit(harness, monkeypatch, tmp_path, problem):
    monkeypatch.setenv("REDLINE_COMPOSE_TESTS", "1")
    monkeypatch.setenv("REDLINE_TEST_RESULTS", str(tmp_path))
    monkeypatch.setattr(harness.importlib.metadata, "version", lambda name: "fixture")
    monkeypatch.setattr(harness, "prepare_sql_server", lambda: None)

    def completed(command, **kwargs):
        junit = next((a.split("=", 1)[1] for a in command if a.startswith("--junitxml=")), None)
        live = junit is not None and Path(junit).name == "live-integrations.xml"
        if junit and not (live and problem == "missing_report"):
            skipped = int(live and problem == "skipped")
            errors = int(live and problem == "errors")
            Path(junit).write_text(
                f'<testsuites><testsuite tests="1" failures="0" errors="{errors}" '
                f'skipped="{skipped}" /></testsuites>'
            )
        return subprocess.CompletedProcess(
            command, 7 if live and problem == "process_failure" else 0
        )

    monkeypatch.setattr(harness.subprocess, "run", completed)
    assert harness.main() == (0 if problem is None else 1)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["qualified"] is (problem is None)
    assert {step["name"] for step in summary["steps"]} >= {
        *harness.PACKAGES,
        "live-integrations",
        "docs",
        "wheel-smoke",
    }
