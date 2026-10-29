"""Check the v0.4 Flask playground with disposable data and no listening sockets."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _assert_store_has_no_value(path, sentinel):
    with sqlite3.connect(path) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (table,) in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            for row in conn.execute(f"SELECT * FROM {quoted}"):
                assert sentinel not in repr(row), f"unredacted sample found in {table}"


def check_production(work, engine):
    from flask import Flask, request

    from flask_redline import Redline

    token = "temporary-demo-token-at-least-24-characters"
    app = Flask("production-preset-smoke")

    @app.post("/inspect")
    def inspect():
        payload = request.get_json(force=True)
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT ?", (payload["notes"],)).scalar_one()
        return {"echo": payload["notes"]}

    @app.post("/inspect-error")
    def inspect_error():
        payload = request.get_json(force=True)
        app.logger.warning("private application log: %s", payload["notes"])
        raise ValueError(payload["notes"])

    extension = Redline(
        app,
        engine=engine,
        settings={
            "preset": "production",
            "token": token,
            "storage_path": str(work / "production-redline.db"),
            "sample_rate": 1.0,
        },
    )
    runtime = app.extensions["redline"]
    cfg = runtime.settings
    assert cfg.read_only and cfg.capture_sql_params is False
    assert not cfg.capture_request_body and not cfg.capture_response_body and not cfg.capture_locals
    assert not cfg.load_from_ui and not cfg.checks_from_ui and not cfg.analysis_from_ui
    assert cfg.capture_error_details is False and cfg.capture_logs is False
    assert cfg.profile == "off"
    assert all(
        not cfg.feature(name)
        for name in ("replay", "replay_writes", "load", "checks", "explain", "profile")
    )
    sentinel = uuid.uuid4().hex
    try:
        with app.test_client() as client:
            assert client.post("/inspect", json={"notes": sentinel}).json == {"echo": sentinel}
            runtime.store.flush()
            trace = runtime.store.get(runtime.store.list(limit=1).items[0]["id"])
            assert trace.request["body"] is None and trace.response["body"] is None
            assert all(s.data.get("params") is None for s in trace.spans if s.kind == "sql")
            assert (
                client.post("/inspect-error", json={"notes": sentinel}, buffered=True).status_code
                == 500
            )
            runtime.store.flush()
            failed = runtime.store.get(runtime.store.list(limit=1).items[0]["id"])
            assert failed.error["type"] == "ValueError"
            assert failed.error["message"] == "[not captured]"
            assert failed.error["traceback"] == "" and not failed.logs
            headers = {"Authorization": "Bearer " + token}
            for path, method in (
                ("api/analysis/sql", "POST"),
                ("api/analysis/demo", "POST"),
                ("api/runs", "POST"),
                ("api/checks/runs", "POST"),
                ("api/traces", "DELETE"),
            ):
                response = client.open("/_redline/" + path, method=method, json={}, headers=headers)
                assert response.status_code in (403, 404), (
                    path,
                    response.status_code,
                    response.json,
                )
                if response.status_code == 404:
                    assert response.json["error"]["code"] == "feature_disabled"
            for path, method, body in (
                ("api/explain", "POST", {"trace_id": trace.id, "span_id": trace.sql_spans()[0].id}),
                (f"api/traces/{trace.id}/profile", "GET", None),
            ):
                refused = client.open(
                    "/_redline/" + path, method=method, json=body, headers=headers
                )
                assert refused.status_code in (403, 404), (path, refused.status_code, refused.json)
            assert client.get("/_redline/api/meta", headers=headers).status_code == 200
        runtime.store.flush()
        _assert_store_has_no_value(work / "production-redline.db", sentinel)
    finally:
        extension.close()
    print("production preset: bodies/locals/SQL values off; active UI operations refused")


def check_driver_guards(work):
    """Exercise refusals with fakes; never connect to an external server."""
    import runpy
    from unittest.mock import patch

    namespace = runpy.run_path(str(ROOT / "examples/flask/driver_probe.py"))
    refused = namespace["ProbeRefused"]
    for vendor in ("mysql", "mssql"):
        calls = []

        class Result:
            def scalar_one(self):
                return "not_a_disposable_database"

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def exec_driver_sql(self, sql, _calls=calls):
                _calls.append(sql)
                return Result()

        class Engine:
            def connect(self):
                return Connection()

            def dispose(self, _calls=calls):
                _calls.append("disposed")

        driver = namespace["DRIVERS"][vendor]
        dsn = f"{driver}://sample:do-not-print@invalid/redline_test_demo"
        with (
            patch("sqlalchemy.create_engine", return_value=Engine()),
            patch("redline_core.contrib.sqlalchemy.instrument_engine"),
        ):
            try:
                namespace["probe"](vendor, dsn, store_path=work / f"guard-{vendor}.db")
            except refused as exc:
                assert str(exc) == "actual_database_is_not_a_named_test_database"
            else:
                raise AssertionError("actual database guard did not refuse")
        assert calls == [
            "SELECT DB_NAME()" if vendor == "mssql" else "SELECT DATABASE()",
            "disposed",
        ]

    env = dict(os.environ)
    for name in namespace["ENVIRON"].values():
        env.pop(name, None)
    command = [sys.executable, str(ROOT / "examples/flask/driver_probe.py")]
    for args in (("--vendor", "mysql"), ("--vendor", "mssql", "--disposable")):
        result = subprocess.run([*command, *args], env=env, capture_output=True, text=True)
        assert result.returncode == 2 and "refused" in json.loads(result.stdout)
    env["REDLINE_DEMO_MYSQL_DSN"] = "mysql+pymysql://sample:do-not-print@invalid/not_a_test"
    result = subprocess.run(
        [*command, "--vendor", "mysql", "--disposable"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2 and "do-not-print" not in result.stdout + result.stderr
    print("external-driver probe guards: explicit consent, URL and actual database checks passed")


def check():
    from sqlalchemy import create_engine

    from redline_core import Settings
    from redline_core.api import RedlineAPI
    from redline_core.check_sql import analyze_sql_source
    from redline_core.sql_compile import compile_paths, compile_statement, schema_snapshot
    from redline_core.storage import SQLiteStore
    from redline_core.testing import assert_traces, capture_traces

    with tempfile.TemporaryDirectory(prefix="redline-real-work-") as directory:
        work = Path(directory)
        os.environ["SHOP_DB"] = str(work / "shop.db")
        os.environ["REDLINE_STORAGE_PATH"] = str(work / "web-redline.db")
        os.environ["REDLINE_PRESET"] = "dev"
        os.environ.pop("SHOP_WORKER_STORE", None)
        sys.path.insert(0, str(ROOT / "examples/flask"))
        from shop.db import sync_engine
        from shop.main import app

        runtime = app.extensions["redline"]
        try:
            with app.test_client() as client:
                headers = {"X-Request-ID": "workflow-request-1", "X-Demo-User-ID": "73"}

                def trace_for(path):
                    runtime.store.flush()
                    items = runtime.store.list(limit=100).items
                    item = next(item for item in items if item["path"] == path)
                    return runtime.store.get(item["id"])

                response = client.get("/workflows/handled-sql", headers=headers)
                assert response.status_code == 200 and response.json["degraded"]
                trace = trace_for("/workflows/handled-sql")
                assert trace.error is None and "sql_error" in trace.issue_kinds
                assert trace.tags["request_id"] == "workflow-request-1"
                assert trace.tags["user_id"] == "73"
                searched = client.get("/_redline/api/traces?q=workflow-request-1").json["items"]
                assert any(item["id"] == trace.id for item in searched)

                with capture_traces("playground.dynamic-engine") as captured:
                    response = client.get("/workflows/dynamic-engine", buffered=True)
                assert response.status_code == 200 and response.json["value"] == 42
                assert_traces(captured, max_queries=1, no_sql_errors=True, no_session_leaks=True)
                dynamic = trace_for("/workflows/dynamic-engine")
                assert dynamic.query_count == 1
                assert next(s for s in dynamic.spans if s.kind == "sql").data["vendor"] == "sqlite"

                sentinel = "invented-private-playground-value"
                response = client.post("/workflows/private", json={"notes": sentinel})
                assert response.status_code == 200 and response.json["payload"]["notes"] == sentinel
                private = trace_for("/workflows/private")
                assert sentinel not in json.dumps(private.to_dict())
                assert json.loads(private.request["body"])["notes"] == "[redacted]"
                assert private.logs and all(log.message == "[redacted]" for log in private.logs)

                failed = client.post("/workflows/job", json={"fail_summary": True}, headers=headers)
                assert failed.status_code == 200 and failed.json["outcome"] == "failed"
                runtime.store.flush()
                task = runtime.store.get(failed.json["trace_id"])
                parent = trace_for("/workflows/job")
                assert task.method == "TASK" and "application_error" in task.issue_kinds
                assert task.tags["parent_trace_id"] == parent.id
                assert task.tags["request_id"] == "workflow-request-1"
                assert task.tags["commit_observed_before_failure"] == "true"
                assert {s.name for s in task.spans if s.data.get("job_stage")} == {
                    "write",
                    "summary",
                }
                assert task.tags["job.row_outcomes"] and "never-recorded" not in json.dumps(
                    task.to_dict()
                )
                succeeded = client.post("/workflows/job", json={"fail_summary": False})
                assert succeeded.status_code == 200 and succeeded.json["outcome"] == "succeeded"
                runtime.store.flush()
                jobs = client.get("/_redline/api/traces?method=TASK").json["items"]
                assert len(jobs) == 2

                source = (ROOT / "examples/flask/sql_samples.py").read_text()
                report = analyze_sql_source(source, dialect="mssql")
                assert {
                    "bind-in-comment",
                    "output-without-into",
                    "session-option-without-reset",
                    "parameter-not-bound",
                } <= {f["kind"] for f in report["findings"]}
                sql_ui = client.post(
                    "/_redline/api/analysis/sql", json={"source": source, "dialect": "mssql"}
                )
                assert sql_ui.status_code == 200 and sql_ui.json["findings"]
                preview = client.post(
                    "/_redline/api/analysis/redaction",
                    json={
                        "sample": {"notes": sentinel},
                        "method": "POST",
                        "route": "/workflows/private",
                    },
                )
                assert preview.status_code == 200 and sentinel not in json.dumps(preview.json)
                health = client.get("/_redline/api/analysis/health")
                assert health.status_code == 200 and health.json["capture"]["engine_mode"] == "all"
                assert health.json["test_traces"] >= 1

            baseline = work / "sql-baseline.json"
            junit = work / "sql-results.xml"
            command = [
                sys.executable,
                "-m",
                "redline_core.cli",
                "check-sql",
                str(ROOT / "examples/flask/sql_samples.py"),
                "--dialect",
                "mssql",
                "--json",
            ]
            initial = subprocess.run(
                [*command, "--write-baseline", str(baseline), "--junit", str(junit)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            assert initial.returncode == 1 and len(json.loads(initial.stdout)["findings"]) == 4
            accepted = subprocess.run(
                [*command, "--baseline", str(baseline)], cwd=ROOT, capture_output=True, text=True
            )
            assert (
                accepted.returncode == 0 and json.loads(accepted.stdout)["baseline_findings"] == 4
            )
            from xml.etree import ElementTree

            assert ElementTree.parse(junit).getroot().get("failures") == "4"
            for operation in ("audit-store", "export"):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "redline_core.cli",
                        operation,
                        "--store",
                        str(work / "web-redline.db"),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                assert (
                    result.returncode in (0, 1)
                    if operation == "audit-store"
                    else result.returncode == 0
                )
                assert sentinel not in result.stdout
                assert json.loads(result.stdout)
            imported = subprocess.run(
                [sys.executable, "-m", "redline_core.cli", "check-imports", "shop.workflows"],
                cwd=ROOT / "examples/flask",
                capture_output=True,
                text=True,
            )
            assert imported.returncode == 0 and json.loads(imported.stdout)["imported"]

            runtime.store.flush()
            _assert_store_has_no_value(work / "web-redline.db", sentinel)
            print(
                "Flask/UI: handled errors, dynamic engines, correlation, privacy, jobs "
                "and SQL tools passed"
            )

            compile_db = work / "compile.db"
            engine = create_engine(f"sqlite:///{compile_db}")
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql("CREATE TABLE items (id INTEGER)")
                assert (
                    compile_statement(engine, "INSERT INTO items VALUES (:id)")["status"]
                    == "compiled"
                )
                assert compile_statement(engine, "SELECT missing FROM items")["status"] == "error"
                with engine.connect() as conn:
                    assert conn.exec_driver_sql("SELECT count(*) FROM items").scalar_one() == 0
                assert "items" in schema_snapshot(engine)["tables"]
            finally:
                engine.dispose()
            source_file = work / "queries.py"
            source_file.write_text(
                'from sqlalchemy import text\nquery = text("SELECT id FROM items")\n'
            )
            snapshot = work / "schema.json"
            results = compile_paths(
                [str(source_file)], f"sqlite:///{compile_db}", snapshot=str(snapshot)
            )
            assert all(item["status"] == "compiled" for item in results)
            assert results[0]["schema_drift"]["added_tables"] == ["items"]
            engine = create_engine(f"sqlite:///{compile_db}")
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE items ADD COLUMN label TEXT")
            engine.dispose()
            changed = compile_paths(
                [str(source_file)], f"sqlite:///{compile_db}", snapshot=str(snapshot)
            )
            assert changed[0]["schema_drift"]["changed_tables"] == ["items"]
            print(
                "SQLite compilation/snapshot: missing column found; "
                "DML not executed; drift reported"
            )

            worker_path = work / "worker-redline.db"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "examples/flask/worker_demo.py"),
                    "--store",
                    str(worker_path),
                ],
                cwd=ROOT / "examples/flask",
                check=True,
                capture_output=True,
                text=True,
            )
            worker_trace_id = json.loads(result.stdout)["trace_id"]
            viewer = SQLiteStore.from_settings(
                Settings(storage_path=str(work / "viewer.db"), extra_stores=(str(worker_path),)),
                background=False,
            )
            try:
                viewed = viewer.get(worker_trace_id)
                assert viewed.tags["secondary_store"] == "1"
                assert viewer.list(limit=10).items[0]["id"] == worker_trace_id
                api = RedlineAPI(viewer)
                sql_span = next(s for s in viewed.spans if s.kind == "sql")
                status, _ = api.dispatch(
                    "POST",
                    "api/explain",
                    body={"trace_id": worker_trace_id, "span_id": sql_span.id},
                )
                assert status == 403  # no accidental use of a web process's unrelated binding
                assert viewer.delete(worker_trace_id) == 0 and viewer.get(worker_trace_id)
            finally:
                viewer.close()
            print(
                "separate worker process/store: job browsed read-only; "
                "wrong-binding EXPLAIN refused"
            )
            check_production(work, sync_engine)
            check_driver_guards(work)
        finally:
            runtime.close()
            sync_engine.dispose()


if __name__ == "__main__":
    check()
