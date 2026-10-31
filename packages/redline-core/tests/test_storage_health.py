from __future__ import annotations

import json
import queue
import sqlite3
import threading

import pytest

from redline_core.storage import SQLiteStore

from .conftest import make_trace


@pytest.mark.parametrize("broken_logger", [False, True])
def test_writer_write_failure_is_counted_and_does_not_leak_message(
    tmp_path, monkeypatch, broken_logger
):
    if broken_logger:

        def fail_log(*args, **kwargs):
            raise RuntimeError("logging unavailable")

        monkeypatch.setattr("redline_core.storage.sqlite.logger.error", fail_log)

    store = SQLiteStore(str(tmp_path / "health.db"), batch_size=1)
    first_write = threading.Event()
    original_write = store._write
    calls = 0

    def fail_once(batch):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_write.set()
            raise sqlite3.OperationalError("private secret")
        original_write(batch)

    store._write = fail_once  # type: ignore[method-assign]
    try:
        store.save(make_trace())
        assert first_write.wait(2)
        store.save(make_trace())
        assert store.flush(2)

        health = store.operational_health()
        assert health["dropped"] == 1
        assert health["written"] == 1
        assert health["dropped_by_reason"] == {"write_failure": 1}
        assert health["write_failures"] == 1
        assert health["last_write_error_type"] == "OperationalError"
        assert "private secret" not in json.dumps(health)
        assert health["status"] == "degraded"
        assert store.count() == 1
    finally:
        assert store.close(timeout=2)


def test_flush_does_not_succeed_for_queue_with_dead_writer(tmp_path):
    store = SQLiteStore(str(tmp_path / "dead-writer.db"))

    class DeadThread:
        def is_alive(self):
            return False

    store._queue = queue.Queue()
    store._queue.put(make_trace())
    store._thread = DeadThread()  # type: ignore[assignment]

    try:
        assert store.flush(timeout=0) is False
        health = store.operational_health()
        assert health["flush_failures"] == 1
        assert health["last_flush_succeeded"] is False
        assert health["queue_depth"] == 1
    finally:
        assert store.close(timeout=0)
    health = store.operational_health()
    assert health["dropped_by_reason"] == {"writer_unavailable": 1}
    assert health["queue_depth"] == 0


def test_close_timeout_counts_queued_drop_and_keeps_connection_until_writer_exits(tmp_path):
    store = SQLiteStore(str(tmp_path / "close-timeout.db"), queue_size=1, batch_size=1)
    writing = threading.Event()
    release = threading.Event()
    original_write = store._write

    def blocked_write(batch):
        writing.set()
        release.wait()
        original_write(batch)

    store._write = blocked_write  # type: ignore[method-assign]
    try:
        store.save(make_trace())
        assert writing.wait(2)
        store.save(make_trace())  # occupies the one queue slot behind the in-flight batch

        assert store.close(timeout=0) is False
        health = store.operational_health()
        assert health["closed"] is True
        assert health["shutdown_complete"] is False
        assert health["dropped_by_reason"] == {"shutdown_timeout": 1}
        assert health["queue_depth"] == 1  # the stop marker remains for the writer

        release.set()
        assert store.close(timeout=2) is True
        health = store.operational_health()
        assert health["shutdown_complete"] is True
        assert health["writer_alive"] is False
        assert health["queue_depth"] == 0
        assert health["written"] == 1
        assert health["dropped"] == 1
    finally:
        release.set()
        if not store.operational_health()["shutdown_complete"]:
            store.close(timeout=2)


def test_concurrent_producers_and_close_account_for_every_save(tmp_path):
    store = SQLiteStore(str(tmp_path / "concurrent-close.db"), queue_size=16, batch_size=4)
    producer_count = 5
    saves_per_producer = 30
    attempts = producer_count * saves_per_producer
    start = threading.Barrier(producer_count + 1)
    close_result: list[bool] = []

    def produce(producer_id: int) -> None:
        start.wait(5)
        for item in range(saves_per_producer):
            trace = make_trace()
            trace.id = f"producer-{producer_id}-{item}"
            store.save(trace)

    def close_store() -> None:
        start.wait(5)
        close_result.append(store.close(timeout=5))

    producers = [threading.Thread(target=produce, args=(i,)) for i in range(producer_count)]
    closer = threading.Thread(target=close_store)
    try:
        for thread in producers:
            thread.start()
        closer.start()
        for thread in producers:
            thread.join(5)
            assert not thread.is_alive()
        closer.join(5)
        assert not closer.is_alive()
        assert close_result == [True]

        health = store.operational_health()
        assert health["shutdown_complete"] is True
        assert health["queue_depth"] == 0
        assert health["writer_alive"] is False
        assert health["written"] + health["dropped"] == attempts
    finally:
        for thread in producers:
            if thread.is_alive():
                thread.join(5)
        if closer.is_alive():
            closer.join(5)
        store.close(timeout=5)


def test_drop_metrics_snapshot_is_exact_under_contention(tmp_path):
    store = SQLiteStore(str(tmp_path / "drop-contention.db"), queue_size=1, batch_size=1)
    writing = threading.Event()
    release = threading.Event()
    original_write = store._write
    producer_count = 40
    barrier = threading.Barrier(producer_count)

    def blocked_write(batch):
        writing.set()
        release.wait()
        original_write(batch)

    def produce() -> None:
        barrier.wait()
        store.save(make_trace())

    store._write = blocked_write  # type: ignore[method-assign]
    try:
        store.save(make_trace())
        assert writing.wait(2)
        store.save(make_trace())  # queue is full while the writer is gated
        producers = [threading.Thread(target=produce) for _ in range(producer_count)]
        for thread in producers:
            thread.start()
        for thread in producers:
            thread.join(2)
            assert not thread.is_alive()

        health = store.operational_health()
        assert health["dropped"] == producer_count
        assert health["dropped_by_reason"] == {"queue_full": producer_count}
        assert sum(health["dropped_by_reason"].values()) == health["dropped"]
    finally:
        release.set()
        flushed = store.flush(2)
        closed = store.close(timeout=2)
        assert flushed
        assert closed


def test_thread_start_failure_can_be_retried(tmp_path, monkeypatch):
    store = SQLiteStore(str(tmp_path / "thread-start.db"))
    original_start = threading.Thread.start
    attempts = 0

    def fail_first_start(thread):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("thread start failed")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_first_start)
    try:
        with pytest.raises(RuntimeError, match="thread start failed"):
            store.save(make_trace())

        store.save(make_trace())
        assert store.flush(2)
        health = store.operational_health()
        assert health["dropped_by_reason"] == {"enqueue_failure": 1}
        assert health["written"] == 1
        assert store.count() == 1
    finally:
        assert store.close(timeout=2)


def test_save_after_close_is_counted_by_reason(tmp_path):
    store = SQLiteStore(str(tmp_path / "closed.db"))
    assert store.close(timeout=2)

    store.save(make_trace())

    health = store.operational_health()
    assert health["closed"] is True
    assert health["dropped"] == 1
    assert health["dropped_by_reason"] == {"closed": 1}
    assert health["status"] == "degraded"
