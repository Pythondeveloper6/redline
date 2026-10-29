"""Same-run relative overhead guards plus deterministic capture-path checks."""

from __future__ import annotations

import time
from contextlib import nullcontext

from redline_core.conf import Settings
from redline_core.recorder import Recorder, current, record_query, record_span, span

N = 500


def _per_call_us(fn) -> float:
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(N):
            fn()
        best = min(best, (time.perf_counter() - t0) / N * 1e6)
    return best


def test_inactive_hooks_are_nearly_free():
    assert current() is None
    t = time.perf_counter()
    baseline = _per_call_us(lambda: None)
    for name, hook in (
        ("record_span", lambda: record_span("sql", "x", t, 0.0)),
        ("record_query", lambda: record_query("SELECT 1", None, start=t, duration=0.0)),
    ):
        assert hook() is None
        cost = _per_call_us(hook)
        # A context lookup/branch may cost several Python calls, but must not perform
        # the bounded normalization, SQL parsing or stack capture of an active trace.
        assert cost / baseline < 12, f"inactive {name}: {cost / baseline:.1f}x a Python call"

    def reference_block():
        with nullcontext():
            pass

    def block():
        with span("x"):
            pass

    ratio = _per_call_us(block) / _per_call_us(reference_block)
    assert ratio < 8, f"inactive span context: {ratio:.1f}x a plain context manager"
    assert current() is None


def test_active_record_query_is_cheap():
    rec = Recorder(Settings(max_spans_per_trace=0, max_trace_bytes=16 * 1024 * 1024))
    builder = rec.start_trace(method="GET", path="/")
    t = time.perf_counter()
    sql = "SELECT * FROM t WHERE id = %s"
    try:
        # Reference includes the same span allocation, callsite walk, bounded data and
        # retention work. Compare in this process rather than using host-specific µs.
        reference = _per_call_us(
            lambda: record_span("custom", "reference", t, 0.0001, {"value": 1})
        )
        cost = _per_call_us(lambda: record_query(sql, [1], start=t, duration=0.0001))
        assert builder.dropped_spans == 0 and not builder.capture_limited
        assert len(builder.spans) == 6 * N
        queries = [item for item in builder.spans if item.kind == "sql"]
        assert len(queries) == 3 * N
        assert all(item.data["params"] == [1] for item in queries)
        assert all(item.callsite is not None for item in queries)
    finally:
        trace = rec.finish_trace(builder, status=200)
    assert trace.query_count == 3 * N
    # Query-specific redaction/fingerprinting should remain a small multiple of the
    # common capture path. Full stack formatting or repeated parsing must not dominate.
    assert cost / reference < 8, f"active query: {cost / reference:.1f}x span capture"
