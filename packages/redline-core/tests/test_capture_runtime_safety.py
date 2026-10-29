"""Observation failures preserve application behavior and refuse unsafe evidence."""

from __future__ import annotations

import importlib
import json
import time

import pytest

from redline_core.conf import Settings
from redline_core.config import resolve_settings
from redline_core.recorder import Recorder, current, record_query
from redline_core.vendors import get_vendor


def production(**overrides):
    return resolve_settings(
        {"preset": "production", "access": "local", "sample_rate": 1, **overrides},
        environ={},
        read_file=False,
    ).settings


@pytest.mark.parametrize("app_fails", [False, True])
def test_finish_failure_cannot_replace_application_result(store, monkeypatch, app_fails):
    sentinel = "application-private-value"
    recorder = Recorder(production(), store)

    def fail(*args, **kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(recorder.capturer, "classify", fail)
    if app_fails:
        with pytest.raises(ValueError, match=sentinel):
            with recorder.recording(method="POST", path=f"/{sentinel}"):
                raise ValueError(sentinel)
    else:
        # resolve_user is deliberately outside the narrower privacy stage.
        monkeypatch.setattr(importlib.import_module("redline_core.privacy"), "resolve_user", fail)
        builder = recorder.start_trace(method="POST", path=f"/{sentinel}")
        recorder.last_trace = recorder.finish_trace(builder, status=200, request_object=object())
    trace = recorder.last_trace
    assert current() is None
    assert trace.path == "/[capture-unavailable]"
    assert trace.truncated["reason"] == "capture_failure"
    assert trace.status == (500 if app_fails else 200)
    assert sentinel not in json.dumps(store.get(trace.id).to_dict())


def test_privacy_failure_never_saves_partially_redacted_trace(store, monkeypatch):
    privacy = importlib.import_module("redline_core.privacy")
    recorder = Recorder(Settings(sample_rate=1), store)
    sentinel = "private-raw-request"

    def fail(trace, settings):
        trace.tags["partial"] = sentinel
        raise RuntimeError(sentinel)

    monkeypatch.setattr(privacy, "sanitize_trace", fail)
    with recorder.recording(method="GET", path=f"/{sentinel}", tags={"secret": sentinel}):
        pass
    trace = store.get(recorder.last_trace.id)
    assert trace.truncated["reason"] == "privacy_failure"
    assert sentinel not in json.dumps(trace.to_dict())


def test_large_query_and_parameters_are_bounded_before_capture(store):
    recorder = Recorder(
        Settings(
            sample_rate=1,
            max_sql_bytes=256,
            max_span_data_bytes=256,
            max_trace_bytes=4096,
            max_tag_bytes=32,
        ),
        store,
    )

    class Hostile:
        def __str__(self):
            raise AssertionError("application string hook called")

        __repr__ = __str__

    with recorder.recording(method="GET", path="/bounded") as builder:
        builder.tag("oversized", "x" * 10000)
        query = record_query(
            "SELECT " + ",".join("?" for _ in range(10000)),
            [Hostile()] * 10000,
            start=time.perf_counter(),
            duration=0,
            capture_callsite=False,
        )
        assert len(query.data["sql"].encode()) <= 256
        assert query.data["capture_incomplete"]
        assert not query.data["explainable"] and not query.data["params_captured"]
    trace = store.get(recorder.last_trace.id)
    assert len(json.dumps(trace.to_dict(), ensure_ascii=False).encode()) <= 4096
    assert trace.truncated["incomplete_evidence"]


def test_vendor_classification_does_not_stringify_application_exception():
    class DriverError(Exception):
        def __str__(self):
            raise AssertionError("application string hook called")

    result = get_vendor("mssql").classify_error(
        DriverError("42S22", "[driver] Invalid column (207) (SQLExecDirectW)")
    )
    assert result["native"] == 207 and result["category"] == "schema"


def test_internal_diagnostics_do_not_expose_exception_values(caplog):
    from redline_core.diagnostics import capture_failure

    try:
        raise RuntimeError("private-application-value")
    except RuntimeError:
        capture_failure("Redline observation failed")
    assert "RuntimeError" in caplog.text
    assert "private-application-value" not in caplog.text


def test_custom_span_bounds_values_without_string_hooks(store):
    from redline_core.recorder import span

    class Hostile:
        def __str__(self):
            raise AssertionError("application hook called")

        __repr__ = __str__

    recorder = Recorder(Settings(sample_rate=1, max_span_data_bytes=256), store)
    cyclic = [Hostile()]
    cyclic.append(cyclic)
    with recorder.recording(method="GET", path="/custom"):
        with span("custom", values=cyclic, giant="x" * 100000):
            with span("child"):
                pass
    trace = recorder.last_trace
    parent = next(s for s in trace.spans if s.name == "custom")
    child = next(s for s in trace.spans if s.name == "child")
    assert child.parent_id == parent.id
    assert trace.truncated["incomplete_evidence"]
    assert len(json.dumps(parent.data, ensure_ascii=True, separators=(",", ":"))) <= 256
    assert "explainable" not in parent.data


def test_custom_span_capture_failure_preserves_original_exception(store, monkeypatch):
    from redline_core.recorder import TraceBuilder, span

    def fail(*args, **kwargs):
        raise RuntimeError("capture failed")

    recorder = Recorder(production(), store)
    monkeypatch.setattr(TraceBuilder, "add", fail)
    original = ValueError("original application error")
    with pytest.raises(ValueError) as raised:
        with recorder.recording(method="GET", path="/custom"):
            with span("custom"):
                raise original
    assert raised.value is original
    assert recorder.last_trace.truncated["capture_errors"] == 1
