import pytest

from redline_core.diagnostics import trace_diagnostics
from redline_core.model import Span, new_id
from tests.conftest import make_trace


def test_overlapping_spans_are_not_added_twice():
    trace = make_trace(
        duration_ms=100,
        spans=[
            Span(new_id(), None, "sql", "query", 0, 60),
            Span(new_id(), None, "sql", "nested query", 10, 10),
            Span(new_id(), None, "http_out", "GET", 40, 40),
        ],
    )
    result = trace_diagnostics(trace)
    assert result["time_ms"] == {
        "database": 40,
        "external": 20,
        "pool_wait": 0,
        "overlap": 20,
        "remaining": 20,
    }
    assert sum(result["time_ms"].values()) == pytest.approx(100)


def test_clipping_and_truncation_are_explicit():
    trace = make_trace(duration_ms=10, spans=[Span(new_id(), None, "sql", "q", -10, 100)])
    trace.truncated = {"dropped_spans": 10, "max_spans": 1}
    result = trace_diagnostics(trace)
    assert result["time_ms"]["database"] == 10
    assert result["warnings"]
    assert not result["complete_spans"]
