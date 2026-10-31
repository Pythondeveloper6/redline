"""RQ integration against a fakeredis-backed queue and an in-process worker."""

import pytest

from redline_core import Recorder, Settings

rq = pytest.importorskip("rq")
fakeredis = pytest.importorskip("fakeredis")

from redline_core.contrib import rq as redline_rq  # noqa: E402


def add(a, b):
    return a + b


def explode():
    raise ValueError("example failure")


@pytest.fixture
def setup(store):
    recorder = Recorder(Settings(capture_logs=False, capture_locals=False), store)
    connection = fakeredis.FakeStrictRedis()
    queue = rq.Queue("example", connection=connection)
    integration = redline_rq.instrument_rq(recorder)
    yield recorder, queue, connection
    integration.uninstall()


def _work(queue, connection):
    worker = rq.SimpleWorker([queue], connection=connection)
    worker.work(burst=True)


def _jobs(store):
    store.flush()
    traces = [store.get(i["id"]) for i in store.list(limit=50).items]
    return [t for t in traces if t.method == "TASK"], [t for t in traces if t.method != "TASK"]


def test_job_is_recorded_and_linked_to_enqueuing_request(setup, store):
    recorder, queue, connection = setup
    with recorder.recording(method="POST", path="/orders", framework="other") as request:
        queue.enqueue(add, 1, 2)
        request_id = request.id
    _work(queue, connection)

    jobs, requests = _jobs(store)
    assert len(requests) == 1 and len(jobs) == 1
    task = jobs[0]
    assert task.tags["job.name"].endswith("test_rq.add")
    assert task.tags["job.queue"] == "example"
    assert task.tags["job.outcome"] == "succeeded"
    assert task.tags["parent_trace_id"] == request_id
    assert len(task.tags["otel_trace_id"]) == 32
    assert task.request["body"] is None


def test_failed_job_records_error_and_outcome(setup, store):
    _recorder, queue, connection = setup
    queue.enqueue(explode)
    _work(queue, connection)
    jobs, _ = _jobs(store)
    assert len(jobs) == 1
    assert jobs[0].tags["job.outcome"] == "failed"
    assert jobs[0].error["type"] == "ValueError"
    # No request was active, so there is nothing to link to.
    assert "parent_trace_id" not in jobs[0].tags


def test_install_is_idempotent_and_uninstall_restores_rq(store):
    recorder = Recorder(Settings(capture_logs=False, capture_locals=False), store)
    original = (rq.Queue.create_job, rq.job.Job.perform)
    first = redline_rq.instrument_rq(recorder)
    assert redline_rq.instrument_rq(recorder) is first
    assert rq.job.Job.perform is not original[1]
    first.uninstall()
    assert (rq.Queue.create_job, rq.job.Job.perform) == original


def test_task_filter_skips_recording(store):
    recorder = Recorder(Settings(capture_logs=False, capture_locals=False), store)
    integration = redline_rq.instrument_rq(recorder, task_filter=lambda name: "add" not in name)
    try:
        connection = fakeredis.FakeStrictRedis()
        queue = rq.Queue("example", connection=connection)
        queue.enqueue(add, 1, 2)
        _work(queue, connection)
        assert _jobs(store)[0] == []
    finally:
        integration.uninstall()
