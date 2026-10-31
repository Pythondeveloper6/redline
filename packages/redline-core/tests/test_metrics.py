from __future__ import annotations

import re

from redline_core.api import RawResponse, RedlineAPI
from redline_core.conf import Settings
from redline_core.metrics import render_metrics
from redline_core.model import Trace
from redline_core.recorder import Recorder
from redline_core.storage import SQLiteStore


def trace(trace_id):
    return Trace(id=trace_id, framework="other", method="GET", path="/x", status=200)


def parse(text):
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            out[name] = float(value)
    return out


def test_exposition_format_and_values(tmp_path):
    store = SQLiteStore(str(tmp_path / "r.db"), background=False)
    store.save(trace("a"))
    store.save(trace("b"))
    text = render_metrics(store)
    store.close()
    assert text.endswith("\n")
    values = parse(text)
    assert values["redline_traces_saved_total"] == 2
    assert values["redline_write_failures_total"] == 0
    assert values['redline_traces_dropped_total{reason="queue_full"}'] == 0
    assert all(re.fullmatch(r"[a-z_]+(\{reason=\"[a-z_]+\"\})?", k) for k in values)
    types = re.findall(r"# TYPE (\S+) (\S+)", text)
    assert ("redline_traces_saved_total", "counter") in types
    assert ("redline_writer_queue_depth", "gauge") in types


def test_drops_by_reason_and_write_failures(tmp_path):
    store = SQLiteStore(str(tmp_path / "r.db"), background=False)
    store.close()
    store.save(trace("late"))  # closed
    store._drop("queue_full", 3)
    store.write_failures = 2
    values = parse(render_metrics(store))
    assert values['redline_traces_dropped_total{reason="closed"}'] == 1
    assert values['redline_traces_dropped_total{reason="queue_full"}'] == 3
    assert values["redline_write_failures_total"] == 2
    assert store.dropped == 4


def test_queue_full_is_counted_with_a_tiny_queue(tmp_path):
    store = SQLiteStore(str(tmp_path / "r.db"), queue_size=1)
    store._ensure_writer()
    import threading

    gate = threading.Event()
    store._write = lambda batch: gate.wait(5)  # type: ignore[method-assign]
    for i in range(20):
        store.save(trace(f"t{i}"))
    values = parse(render_metrics(store))
    assert values['redline_traces_dropped_total{reason="queue_full"}'] >= 1
    assert values["redline_writer_queue_depth"] <= 1
    gate.set()
    store.close()


def test_recorder_capture_failures(tmp_path):
    store = SQLiteStore(str(tmp_path / "r.db"), background=False)
    rec = Recorder(Settings(), store)
    rec.dropped_capture = 4
    values = parse(render_metrics(store, rec))
    store.close()
    assert values['redline_traces_dropped_total{reason="capture_failed"}'] == 4


def test_store_without_health_still_renders():
    class Plain:
        dropped = 5
        written = 7

    values = parse(render_metrics(Plain()))
    assert values['redline_traces_dropped_total{reason="other"}'] == 5
    assert values["redline_traces_saved_total"] == 7


def test_api_route_is_get_only_raw_text(tmp_path):
    store = SQLiteStore(str(tmp_path / "r.db"), background=False)
    api = RedlineAPI(store, Settings(storage_path=str(tmp_path / "r.db")))
    status, body = api.dispatch("GET", "api/metrics")
    assert status == 200 and isinstance(body, RawResponse)
    assert body.content_type.startswith("text/plain; version=0.0.4")
    assert b"redline_traces_saved_total" in body.body
    assert api.dispatch("POST", "api/metrics")[0] == 405
    store.close()
