"""Dramatiq integration on the in-memory StubBroker."""

import pytest

from redline_core import Recorder, Settings

dramatiq = pytest.importorskip("dramatiq")

from dramatiq import Worker  # noqa: E402
from dramatiq.brokers.stub import StubBroker  # noqa: E402

from redline_core.contrib.dramatiq import instrument_dramatiq  # noqa: E402


@pytest.fixture
def setup(store):
    recorder = Recorder(Settings(capture_logs=False, capture_locals=False), store)
    broker = StubBroker()
    broker.emit_after("process_boot")
    middleware = instrument_dramatiq(broker, recorder)

    @dramatiq.actor(broker=broker, actor_name="example_ok", max_retries=0)
    def ok(value):
        return value

    @dramatiq.actor(broker=broker, actor_name="example_fail", max_retries=0)
    def fail():
        raise ValueError("example failure")

    worker = Worker(broker, worker_timeout=100)
    worker.start()
    yield recorder, broker, ok, fail, middleware
    worker.stop()
    broker.close()


def _jobs(store):
    store.flush()
    traces = [store.get(i["id"]) for i in store.list(limit=50).items]
    return [t for t in traces if t.method == "TASK"], [t for t in traces if t.method != "TASK"]


def test_message_is_recorded_and_linked_to_enqueuing_request(setup, store):
    recorder, broker, ok, _, _ = setup
    with recorder.recording(method="POST", path="/orders", framework="other") as request:
        ok.send(1)
        request_id = request.id
    broker.join(ok.queue_name)
    jobs, requests = _jobs(store)
    assert len(requests) == 1 and len(jobs) == 1
    task = jobs[0]
    assert task.tags["job.name"] == "example_ok"
    assert task.tags["job.queue"] == "default"
    assert task.tags["job.outcome"] == "succeeded"
    assert task.tags["parent_trace_id"] == request_id
    assert len(task.tags["otel_trace_id"]) == 32


def test_failed_message_records_error(setup, store):
    _, broker, _, fail, _ = setup
    fail.send()
    broker.join(fail.queue_name, fail_fast=False)
    jobs, _ = _jobs(store)
    assert len(jobs) == 1
    assert jobs[0].tags["job.outcome"] == "failed"
    assert jobs[0].error["type"] == "ValueError"


def test_install_is_idempotent_and_uninstall_removes_middleware(setup):
    _, broker, _, _, middleware = setup
    assert instrument_dramatiq(broker) is middleware
    middleware.uninstall(broker)
    assert middleware not in broker.middleware
