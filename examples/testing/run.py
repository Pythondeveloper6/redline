"""Run all suites in the owned Compose environment and preserve honest failure reports."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    "redline-core",
    "django-redline",
    "fastapi-redline",
    "flask-redline",
    "litestar-redline",
)


def prepare_sql_server():
    """Provision only the fixed disposable DB on the Compose service, never an external DSN."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    if os.environ.get("REDLINE_COMPOSE_TESTS") != "1":
        raise RuntimeError("Disposable Compose opt-in is required")
    url = make_url(os.environ["REDLINE_TEST_MSSQL_DSN"])
    if url.host != "mssql" or url.database != "redline_test_integration":
        raise RuntimeError("Refusing SQL Server provisioning outside the Compose test database")
    engine = create_engine(
        url.set(database="master"), isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "IF DB_ID(N'redline_test_integration') IS NULL "
                "CREATE DATABASE [redline_test_integration]"
            )
    finally:
        engine.dispose()


def report_counts(file):
    root = ET.parse(file).getroot()
    suites = list(root.iter("testsuite"))
    return {
        key: sum(int(s.get(key, "0")) for s in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }


def main():
    if os.environ.get("REDLINE_COMPOSE_TESTS") != "1":
        print("Refusing integration runner without REDLINE_COMPOSE_TESTS=1", flush=True)
        return 2
    root_results = Path(os.environ.get("REDLINE_TEST_RESULTS", "/results"))
    root_results.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    results = root_results / run_id
    results.mkdir()
    report = {
        "qualified": False,
        "run_id": run_id,
        "steps": [],
        "versions": {},
        "frontend": "npm tests and TypeScript/build are required by the image build",
    }

    def save_report():
        payload = json.dumps(report, indent=2)
        (results / "summary.json").write_text(payload)
        (root_results / "summary.json").write_text(payload)

    save_report()
    try:
        return execute(results, report, save_report)
    except Exception as exc:
        report["steps"].append(
            {"name": "runner_failure", "exit_code": 1, "error_type": type(exc).__name__}
        )
        save_report()
        print("Test runner failed; inspect the current summary.json", flush=True)
        return 1


def execute(results, report, save_report):
    for package in (
        "django",
        "sqlalchemy",
        "celery",
        "pymysql",
        "pyodbc",
        "aioodbc",
        "mysqlclient",
        "mssql-django",
    ):
        report["versions"][package] = importlib.metadata.version(package)

    def run(name, command, cwd=ROOT, *, junit=None):
        print(f"\nRunning {name}", flush=True)
        started = time.monotonic()
        with (results / f"{name}.log").open("w") as log:
            completed = subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        entry = {
            "name": name,
            "exit_code": completed.returncode,
            "seconds": round(time.monotonic() - started, 2),
        }
        if junit:
            if not junit.is_file():
                entry.update(exit_code=1, error="missing_junit_report")
            else:
                entry["counts"] = report_counts(junit)
                if any(entry["counts"][key] for key in ("failures", "errors", "skipped")):
                    entry["exit_code"] = 1
        report["steps"].append(entry)
        save_report()
        print(name, "PASS" if entry["exit_code"] == 0 else "FAIL", flush=True)
        return entry["exit_code"]

    try:
        prepare_sql_server()
    except Exception as exc:
        report["steps"].append(
            {"name": "database_setup", "exit_code": 1, "error_type": type(exc).__name__}
        )
        save_report()
        print("Compose database setup failed; inspect service health and summary.json", flush=True)
        return 1

    run("lint", ["ruff", "check", "."])
    run("format", ["ruff", "format", "--check", "."])
    run("versions", [sys.executable, "scripts/check_versions.py"])
    for package in PACKAGES:
        junit = results / (package + ".xml")
        run(
            package,
            [sys.executable, "-m", "pytest", "-ra", "--junitxml=" + str(junit)],
            ROOT / "packages" / package,
            junit=junit,
        )
    junit = results / "live-integrations.xml"
    run(
        "live-integrations",
        [
            sys.executable,
            "-m",
            "pytest",
            "examples/testing/tests",
            "-ra",
            "--junitxml=" + str(junit),
        ],
        junit=junit,
    )
    for script in ("check_demo_patterns", "check_real_world_workflows"):
        run(script, [sys.executable, "scripts/" + script + ".py"])
    run(
        "docs",
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", "/tmp/redline-site"],
    )
    dist = results / "dist"
    if not run(
        "packages",
        ["uv", "build", "--all-packages", "--no-build-isolation", "--out-dir", str(dist)],
    ):
        artifacts = sorted(str(p) for p in dist.glob("*") if p.suffix in (".whl", ".gz"))
        run("metadata", ["twine", "check", *artifacts])
        wheelenv = "/tmp/redline-wheelcheck"
        if not run("wheel-venv", ["uv", "venv", "--python", sys.executable, wheelenv]):
            wheelpython = wheelenv + "/bin/python"
            if not run(
                "wheel-install",
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    wheelpython,
                    *sorted(str(p) for p in dist.glob("*.whl")),
                ],
            ):
                run("wheel-smoke", [wheelpython, "scripts/smoke_wheels.py"])
    report["qualified"] = bool(report["steps"]) and all(
        s["exit_code"] == 0 for s in report["steps"]
    )
    save_report()
    print(
        "All requested checks passed" if report["qualified"] else "Checks failed; inspect /results",
        flush=True,
    )
    return 0 if report["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
