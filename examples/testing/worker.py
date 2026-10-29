"""Disposable Redis/prefork fixture; never import into an application deployment.

Required environment (provided by the Compose test runner):
REDLINE_TEST_COMPOSE=1, REDLINE_TEST_REDIS_URL=redis://redis:6379/1,
REDLINE_TEST_WORKER_STORE=/tmp/redline-worker/traces.db (absolute shared local path).
The worker owns a separate ``*.application.db`` next to its recording store. Each child
opens its recorder, SQLite connections and engine after fork, and closes them on shutdown.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from celery import Celery, signals
from sqlalchemy import create_engine

from redline_core import Recorder, Settings, row_outcome, stage
from redline_core.contrib.celery import instrument_celery
from redline_core.contrib.sqlalchemy import instrument_engine
from redline_core.storage import SQLiteStore

if os.environ.get("REDLINE_TEST_COMPOSE") != "1":
    raise RuntimeError("The disposable worker requires REDLINE_TEST_COMPOSE=1")

_store_path = Path(os.environ["REDLINE_TEST_WORKER_STORE"])
if not _store_path.is_absolute():
    raise RuntimeError("REDLINE_TEST_WORKER_STORE must be an absolute local path")
_broker = os.environ["REDLINE_TEST_REDIS_URL"]
if not _broker.startswith(("redis://", "rediss://")):
    raise RuntimeError("REDLINE_TEST_REDIS_URL must select the disposable Redis broker")

app = Celery("redline-compose-worker", broker=_broker, backend=_broker)
app.conf.update(
    task_always_eager=False,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    result_expires=300,
)
_runtime = None


@signals.worker_process_init.connect(weak=False)
def initialize_child(**kwargs):
    global _runtime
    _store_path.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        storage_path=str(_store_path),
        sample_rate=1.0,
        capture_logs=False,
        capture_locals=False,
        capture_error_details=False,
    )
    store = SQLiteStore.from_settings(settings)
    recorder = Recorder(settings, store)
    engine = create_engine("sqlite:///" + str(_store_path.with_suffix(".application.db")))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS fixture_rows (value INTEGER)")
    instrument_engine(engine, alias="worker_fixture")
    integration = instrument_celery(app, recorder)
    _runtime = store, engine, integration


@signals.worker_process_shutdown.connect(weak=False)
def shutdown_child(pid=None, **kwargs):
    global _runtime
    if _runtime is None:
        return
    store, engine, integration = _runtime
    _runtime = None
    integration.uninstall()
    engine.dispose()
    drained = store.flush(15)
    report = {
        "pid": os.getpid(),
        "flushed": drained,
        "dropped": store.dropped,
        "written": store.written,
    }
    store.close()
    # A unique PID report proves this child's graceful cleanup ran; it contains no
    # task values, connection URLs or application data.
    _store_path.with_name(_store_path.name + f".shutdown-{os.getpid()}.json").write_text(
        json.dumps(report)
    )


def _write_rows(count):
    if _runtime is None:
        raise RuntimeError("Worker process initialization did not run")
    _, engine, _ = _runtime
    with stage("write"):
        with engine.begin() as connection:
            for _ in range(count):
                connection.exec_driver_sql("INSERT INTO fixture_rows (value) VALUES (1)")
                row_outcome("succeeded", "fixture-written")
            rows = connection.exec_driver_sql("SELECT value FROM fixture_rows LIMIT 2").fetchall()
            assert rows


@app.task(name="redline.compose.success")
def success():
    _write_rows(2)
    with stage("summary"):
        return {"rows": 2, "pid": os.getpid()}


@app.task(name="redline.compose.failure")
def failure():
    _write_rows(1)
    with stage("summary"):
        raise ValueError("Disposable summary failed after commit")


@app.task(bind=True, name="redline.compose.retry", max_retries=1)
def retry(task):
    with stage("attempt"):
        if task.request.retries == 0:
            row_outcome("parked", "fixture-retry")
            raise task.retry(countdown=0)
        _write_rows(1)
        return {"attempt": task.request.retries, "pid": os.getpid()}
