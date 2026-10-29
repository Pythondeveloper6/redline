"""Actual Redis delivery and prefork worker lifecycle, only in disposable Compose.

This suite intentionally runs separately from package tests. The runner supplies
REDLINE_TEST_COMPOSE=1, REDLINE_TEST_REDIS_URL and REDLINE_TEST_WORKER_STORE.
It starts/stops its own worker process group, never purges Redis or controls other workers.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("REDLINE_TEST_COMPOSE") != "1", reason="disposable Compose opt-in required"
)
ROOT = Path(__file__).resolve().parents[3]


def _stop(process):
    if process.poll() is not None:
        # A crashed leader can leave its prefork children alive in the session.
        # This group was created exclusively for this Popen invocation.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return
    # Celery's main process performs a warm shutdown on TERM and joins its pool.
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        # The dedicated session contains only this fixture's parent/prefork children.
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        raise AssertionError("Owned worker did not finish its warm shutdown") from None


@pytest.fixture
def live_worker(tmp_path):
    if sys.platform == "win32":
        pytest.fail("This Compose suite requires a POSIX prefork worker")
    for variable in ("REDLINE_TEST_REDIS_URL", "REDLINE_TEST_WORKER_STORE"):
        if not os.environ.get(variable):
            pytest.fail(f"Compose runner did not provide {variable}")
    store = Path(os.environ["REDLINE_TEST_WORKER_STORE"])
    assert store.is_absolute(), "Worker recording store must be an absolute local path"
    try:
        __import__("celery")
    except ImportError:
        pytest.fail("Opted-in integration environment must include Celery")
    # The source checkout is the CLI subprocess's working directory too.
    sys.path.insert(0, str(ROOT))
    from examples.testing.worker import app

    identifier = uuid.uuid4().hex
    node = f"redline-compose-{identifier}@fixture"
    queue = f"redline-compose-{identifier}"
    logfile = tmp_path / "worker.log"
    process = None
    with logfile.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "examples.testing.worker:app",
                "worker",
                "--pool=prefork",
                "--concurrency=1",
                "--hostname",
                node,
                "--queues",
                queue,
                "--loglevel=WARNING",
                "--without-gossip",
                "--without-mingle",
            ],
            cwd=ROOT,
            env=dict(os.environ),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"Owned worker exited before readiness; inspect {logfile}")
                try:
                    replies = app.control.ping(destination=[node], timeout=1)
                except Exception:
                    replies = []
                if any(reply.get(node, {}).get("ok") == "pong" for reply in replies):
                    break
                time.sleep(0.2)
            else:
                pytest.fail(f"Owned worker did not answer readiness ping; inspect {logfile}")
            yield app, process, store, queue, identifier
        finally:
            try:
                _stop(process)
            finally:
                app.close()
                sys.path.remove(str(ROOT))


def test_redis_prefork_recordings_success_failure_retry(live_worker):
    from redline_core import Recorder, Settings, tag
    from redline_core.contrib.celery import instrument_celery
    from redline_core.storage.multiple import TraceReader

    app, process, path, queue, identifier = live_worker
    assert not app.conf.task_always_eager
    publisher = Recorder(Settings(capture_logs=False, capture_locals=False))
    integration = instrument_celery(app, publisher)
    request_id = "fixture-request-" + identifier
    ids = {name: identifier + "-" + name for name in ("success", "failure", "retry")}
    results = []
    try:
        with publisher.recording(method="POST", path="/fixture/publish") as parent:
            tag("request_id", request_id)
            for name in ids:
                results.append(
                    app.send_task("redline.compose." + name, task_id=ids[name], queue=queue)
                )
        succeeded = results[0].get(timeout=60)
        assert succeeded["rows"] == 2
        assert results[1].get(timeout=60, propagate=False).__class__.__name__ == "ValueError"
        assert results[1].failed()
        retried = results[2].get(timeout=60)
        assert retried["attempt"] == 1
        child_pids = {succeeded["pid"], retried["pid"]}
        assert process.pid not in child_pids and os.getpid() not in child_pids
        # Stored traces are read only after warm shutdown flushes the prefork child's writer.
        _stop(process)
        reports = [
            json.loads(p.read_text()) for p in path.parent.glob(path.name + ".shutdown-*.json")
        ]
        reports = [report for report in reports if report["pid"] in child_pids]
        assert {report["pid"] for report in reports} == child_pids
        assert sum(report["written"] for report in reports) >= 4
        assert all(report["flushed"] and report["dropped"] == 0 for report in reports)
        reader = TraceReader(path)
        try:
            recordings = [reader.get(item["id"]) for item in reader.list(limit=500).items]
        finally:
            reader.close()
        tasks = [trace for trace in recordings if trace.tags.get("job.id") in ids.values()]
        assert len(tasks) == 4
        assert all(t.method == "TASK" and not t.truncated for t in tasks)
        assert all(t.tags["request_id"] == request_id for t in tasks)
        assert {t.tags["job.outcome"] for t in tasks} == {"succeeded", "failed", "retry"}
        retry_attempts = [t for t in tasks if t.tags["job.id"] == ids["retry"]]
        assert {t.tags["job.attempt"] for t in retry_attempts} == {"0", "1"}
        first_retry = next(t for t in retry_attempts if t.tags["job.attempt"] == "0")
        second_retry = next(t for t in retry_attempts if t.tags["job.attempt"] == "1")
        assert first_retry.tags["parent_trace_id"] == parent.id
        assert second_retry.tags["parent_trace_id"] == first_retry.id
        assert all(
            t.tags["parent_trace_id"] == parent.id
            for t in tasks
            if t.tags["job.id"] != ids["retry"]
        )
        failed = next(t for t in tasks if t.tags["job.id"] == ids["failure"])
        assert failed.error["type"] == "ValueError"
        assert "application_error" in failed.issue_kinds
        assert failed.tags["commit_observed_before_failure"] == "true"
        success = next(t for t in tasks if t.tags["job.id"] == ids["success"])
        assert {s.name for s in success.spans if s.data.get("job_stage")} >= {"write", "summary"}
        assert sum(json.loads(success.tags["job.row_outcomes"]).values()) == 2
        assert any((s.data.get("rows") or 0) >= 1 for s in success.sql_spans())
        assert all("args" not in t.request and "result" not in t.request for t in tasks)
    finally:
        integration.uninstall()
        for result in results:
            result.forget()
