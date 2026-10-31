import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.operational import health_snapshot
from redline_core.recorder import Recorder


def test_health_endpoint_does_not_query_store(store, monkeypatch):
    recorder = Recorder(Settings(), store)
    api = RedlineAPI(store, runtime_health=recorder.operational_health)

    def fail(*args, **kwargs):
        raise AssertionError("must not query SQLite")

    monkeypatch.setattr(store, "count", fail)
    monkeypatch.setattr(store, "_query", fail)
    code, health = api.dispatch("GET", "api/health")
    assert code == 200
    assert health["status"] == "ok"
    assert health["storage"]["queue_depth"] == 0
    assert health["recorder"]["capture_failures"] == 0
    assert api.dispatch("POST", "api/health")[0] == 405


def test_capture_failures_are_visible_and_concurrency_safe(store):
    recorder = Recorder(Settings(), store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: recorder._note_capture_loss(), range(100)))
    api = RedlineAPI(store, runtime_health=recorder.operational_health)
    code, health = api.dispatch("GET", "api/health")
    assert code == 503
    assert health["recorder"]["capture_failures"] == 100
    assert recorder.dropped_capture == 100
    assert "process" in health["note"]


def test_unsupported_or_failing_health_is_not_reported_as_healthy():
    def fail():
        raise RuntimeError("private health details")

    health = health_snapshot(SimpleNamespace(operational_health=fail), fail)
    assert health["status"] == "degraded"
    assert not health["storage"]["available"]
    assert "private health details" not in json.dumps(health)
    assert health_snapshot(object())["recorder"]["status"] == "unknown"


def test_health_filters_unknown_custom_store_fields():
    store = SimpleNamespace(
        operational_health=lambda: {
            "status": "ok",
            "secret": "must not escape",
            "path": "/private/store.db",
            "dropped_by_reason": {"closed": 1, "private-value": "secret"},
        }
    )
    health = health_snapshot(store, lambda: {"status": "ok"})
    assert "must not escape" not in json.dumps(health)
    assert "/private/" not in json.dumps(health)
    assert health["storage"]["dropped_by_reason"] == {"closed": 1}


def test_recorder_counters_reset_for_a_new_process():
    recorder = Recorder(Settings())
    recorder._note_capture_loss()
    recorder._counter_pid = -1
    assert recorder.operational_health()["capture_failures"] == 0


@pytest.mark.parametrize("details", [False, True])
def test_internal_api_errors_withhold_details_when_capture_is_disabled(store, details, monkeypatch):
    secret = "postgresql://user:private-password@db/customer"
    api = RedlineAPI(store, Settings(capture_error_details=details))

    def fail(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(api, "_route", fail)
    code, data = api.dispatch("GET", "api/traces")
    assert code == 500 and data["error"]["code"] == "internal_error"
    message = data["error"]["message"]
    assert "private-password" not in message  # redacted even when details are shown
    assert ("db/customer" in message) is details


def test_counter_fork_handler_resets_inherited_locked_counter():
    import os
    import select
    import signal

    if not hasattr(os, "fork"):
        pytest.skip("requires POSIX fork")
    recorder = Recorder(Settings())
    recorder._note_capture_loss()
    read_fd, write_fd = os.pipe()
    recorder._counter_lock.acquire()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            health = recorder.operational_health()
            os.write(write_fd, json.dumps(health).encode())
        finally:
            os.close(write_fd)
            os._exit(0)
    recorder._counter_lock.release()
    os.close(write_fd)
    try:
        ready, _, _ = select.select([read_fd], [], [], 5)
        if not ready:
            os.kill(child, signal.SIGKILL)
        assert ready, "child health inherited a locked counter"
        health = json.loads(os.read(read_fd, 4096))
        assert health["pid"] == child
        assert health["capture_failures"] == 0
    finally:
        os.close(read_fd)
        os.waitpid(child, 0)
    assert recorder.operational_health()["capture_failures"] == 1
