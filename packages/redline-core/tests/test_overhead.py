"""Micro-benchmark sanity checks: generous bounds that catch regressions by orders of
magnitude (e.g. an accidental full stack format per query), not precise numbers."""

from __future__ import annotations

import time

from redline_core.conf import Settings
from redline_core.recorder import Recorder, record_query, record_span, span

N = 5_000


def _per_call_us(fn) -> float:
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(N):
            fn()
        best = min(best, (time.perf_counter() - t0) / N * 1e6)
    return best


def test_inactive_hooks_are_nearly_free():
    t = time.perf_counter()
    cost = _per_call_us(lambda: record_span("sql", "x", t, 0.0))
    assert cost < 2.0, f"inactive record_span costs {cost:.2f} µs"
    cost = _per_call_us(lambda: record_query("SELECT 1", None, start=t, duration=0.0))
    assert cost < 2.0, f"inactive record_query costs {cost:.2f} µs"

    def block():
        with span("x"):
            pass

    assert _per_call_us(block) < 5.0


def test_active_record_query_is_cheap():
    rec = Recorder(Settings())
    b = rec.start_trace(method="GET", path="/")
    t = time.perf_counter()
    try:
        cost = _per_call_us(
            lambda: record_query("SELECT * FROM t WHERE id = %s", [1], start=t, duration=0.0001)
        )
    finally:
        b.spans.clear()
        rec.finish_trace(b, status=200)
    # fingerprint is memoised; callsite capture walks the stack once per query
    assert cost < 60.0, f"active record_query costs {cost:.2f} µs"
