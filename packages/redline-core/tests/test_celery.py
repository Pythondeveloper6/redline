"""Runs with the optional Celery dependency; eager mode needs no broker or sockets."""

import pytest

from redline_core import Recorder, Settings
from redline_core.contrib.celery import instrument_celery

celery = pytest.importorskip("celery")


def test_eager_success_failure_retry(store):
    app = celery.Celery("redline-integration", broker="memory://", backend="cache+memory://")
    app.conf.update(task_always_eager=True, task_store_eager_result=False)
    recorder = Recorder(Settings(capture_logs=False, capture_locals=False), store)
    integration = instrument_celery(app, recorder)
    assert instrument_celery(app, recorder) is integration

    @app.task(name="redline.success")
    def success():
        return 3

    @app.task(name="redline.failure")
    def failure():
        raise ValueError("example failure")

    @app.task(bind=True, name="redline.retry")
    def retry(task):
        if task.request.retries == 0:
            raise task.retry(countdown=0, max_retries=1)
        return 4

    try:
        assert success.delay().get() == 3
        result = failure.delay()
        assert result.failed()
        assert retry.delay().get() == 4
        store.flush()
        traces = [store.get(i["id"]) for i in store.list(limit=20).items]
        assert len(traces) == 4
        assert {t.tags["job.outcome"] for t in traces} == {"succeeded", "failed", "retry"}
        failed = next(t for t in traces if t.tags["job.outcome"] == "failed")
        assert failed.error["type"] == "ValueError"
        assert "application_error" in failed.issue_kinds
        assert all(t.request["body"] is None for t in traces)
    finally:
        integration.uninstall()
        app.close()
