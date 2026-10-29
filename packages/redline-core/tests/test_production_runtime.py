"""Runtime privacy and deterministic storage-failure checks for the production preset."""

from __future__ import annotations

import importlib
import logging
import sqlite3
import threading
import time
import uuid

import pytest

from redline_core.config import resolve_settings
from redline_core.recorder import Recorder, configure, record_query
from redline_core.storage import SQLiteStore


def production_settings(path):
    return resolve_settings(
        {"preset": "production", "access": "local", "storage_path": str(path), "sample_rate": 1.0},
        environ={},
        read_file=False,
    ).settings


def assert_absent_from_store(path, value):
    with sqlite3.connect(path) as connection:
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            quoted = '"' + table.replace('"', '""') + '"'
            assert all(
                value not in repr(row) for row in connection.execute(f"SELECT * FROM {quoted}")
            )


def test_production_runtime_omits_error_logs_bodies_and_sql_values(tmp_path):
    path = tmp_path / "private.db"
    settings = production_settings(path)
    assert not settings.capture_error_details and not settings.capture_logs
    assert all(
        not settings.feature(name)
        for name in ("replay", "replay_writes", "load", "checks", "explain", "profile")
    )
    sentinel = uuid.uuid4().hex
    with SQLiteStore(path, background=False) as store:
        recorder = Recorder(settings, store)
        with pytest.raises(ValueError, match=sentinel):
            with recorder.recording(method="POST", path="/private", body=sentinel):
                logging.getLogger("production.fixture").warning("notes: %s", sentinel)
                record_query(
                    f"SELECT '{sentinel}', ?",
                    [sentinel],
                    start=time.perf_counter(),
                    duration=0,
                    capture_callsite=False,
                )
                raise ValueError(sentinel)
        trace = store.get(recorder.last_trace.id)
        assert trace.status == 500 and trace.error["type"] == "ValueError"
        assert trace.error["message"] == "[not captured]" and trace.error["traceback"] == ""
        assert not trace.logs and not trace.profile
        assert trace.request["body"] is None
        assert trace.sql_spans()[0].data["params"] is None
        assert_absent_from_store(path, sentinel)


def test_reconfigure_does_not_change_an_active_recorders_capture_policy(tmp_path, monkeypatch):
    module = importlib.import_module("redline_core.recorder")
    monkeypatch.setattr(module, "_default", module._default)
    dev_settings = resolve_settings(
        {"preset": "dev", "capture_logs": True, "log_level": "WARNING", "sample_rate": 1.0},
        environ={},
        read_file=False,
    ).settings
    sentinel = uuid.uuid4().hex
    with SQLiteStore(tmp_path / "dev.db", background=False) as dev_store:
        with SQLiteStore(tmp_path / "prod.db", background=False) as prod_store:
            dev = configure(dev_settings, dev_store)
            builder = dev.start_trace(method="GET", path="/dev")
            prod = configure(production_settings(tmp_path / "prod.db"), prod_store)
            logging.getLogger("production.fixture").warning("notes: %s", sentinel)
            dev_trace = dev.finish_trace(builder, status=500, error=ValueError(sentinel))
            assert sentinel in dev_trace.error["message"]
            assert any(sentinel in entry.message for entry in dev_trace.logs)
            assert dev_store.get(dev_trace.id) is not None and prod_store.get(dev_trace.id) is None
            with pytest.raises(ValueError), prod.recording(method="GET", path="/prod"):
                logging.getLogger("production.fixture").warning("notes: %s", sentinel)
                raise ValueError(sentinel)
            assert not prod.last_trace.logs
            assert_absent_from_store(tmp_path / "prod.db", sentinel)


def test_full_storage_queue_drops_without_breaking_recording(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "queue.db", queue_size=1, batch_size=1)
    recorder = Recorder(production_settings(tmp_path / "queue.db"), store)
    started, release = threading.Event(), threading.Event()
    original_write = store._write

    def blocked_write(batch):
        started.set()
        assert release.wait(5), "test did not release the blocked writer"
        original_write(batch)

    monkeypatch.setattr(store, "_write", blocked_write)
    try:
        with recorder.recording(method="GET", path="/first"):
            pass
        first = recorder.last_trace.id
        assert started.wait(5), "writer did not start"
        with recorder.recording(method="GET", path="/second"):
            pass
        second = recorder.last_trace.id
        with recorder.recording(method="GET", path="/overflow"):
            pass
        overflow = recorder.last_trace.id
        assert store.dropped == 1
        release.set()
        assert store.flush()
        assert store.get(first) is not None and store.get(second) is not None
        assert store.get(overflow) is None
    finally:
        release.set()
        store.close()


def test_synchronous_disk_failure_does_not_escape_request(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "full.db", background=False) as store:
        recorder = Recorder(production_settings(tmp_path / "full.db"), store)

        def fail_write(_batch):
            raise sqlite3.OperationalError("database or disk is full")

        monkeypatch.setattr(store, "_write", fail_write)
        with recorder.recording(method="GET", path="/still-works"):
            pass
        assert recorder.last_trace.status == 200
        assert store.get(recorder.last_trace.id) is None


def test_background_disk_failure_is_counted_and_writer_recovers(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "recover.db", batch_size=1) as store:
        recorder = Recorder(production_settings(tmp_path / "recover.db"), store)
        original_write = store._write
        failed = threading.Event()

        def fail_once(batch):
            if not failed.is_set():
                failed.set()
                raise sqlite3.OperationalError("database or disk is full")
            original_write(batch)

        monkeypatch.setattr(store, "_write", fail_once)
        with recorder.recording(method="GET", path="/lost"):
            pass
        lost = recorder.last_trace.id
        assert store.flush()
        assert failed.is_set()
        with recorder.recording(method="GET", path="/recovered"):
            pass
        recovered = recorder.last_trace.id
        assert store.flush()
        assert store.get(lost) is None and store.get(recovered) is not None
        assert store.dropped == 1
