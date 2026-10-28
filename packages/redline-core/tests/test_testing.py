import pytest

from redline_core.conf import Settings
from redline_core.recorder import Recorder
from redline_core.testing import assert_traces, capture_traces


def test_capture_keeps_sampled_out_requests_and_restores_context():
    recorder = Recorder(Settings(sample_rate=0, always_keep_slower_than_ms=0))
    with capture_traces("test_example") as captured:
        trace = recorder.start_trace(method="GET", path="/example")
        recorder.finish_trace(trace, status=200)
    assert len(captured) == 1
    assert captured[0].tags["test_name"] == "test_example"
    assert_traces(captured, max_queries=0)
    recorder.finish_trace(recorder.start_trace(method="GET", path="/other"), status=200)
    assert len(captured) == 1


def test_no_instrumentation_cannot_pass_silently():
    with pytest.raises(AssertionError, match="No instrumented requests"):
        assert_traces([], max_queries=0)


@pytest.mark.redline(max_queries=0, no_n_plus_one=True)
def test_marker_captures_requests():
    recorder = Recorder(Settings())
    recorder.finish_trace(recorder.start_trace(method="GET", path="/example"), status=200)


def test_no_query_block_reports_access_and_preserves_original_errors():
    from redline_core.recorder import record_query
    from redline_core.testing import assert_no_queries

    recorder = Recorder(Settings())
    builder = recorder.start_trace(method="GET", path="/example")
    try:
        with pytest.raises(AssertionError, match="Unexpected SQL"):
            with assert_no_queries():
                record_query("SELECT 1", [], start=builder.t0, duration=0.001)
        with pytest.raises(ValueError, match="original"):
            with assert_no_queries():
                record_query("SELECT 1", [], start=builder.t0, duration=0.001)
                raise ValueError("original")
    finally:
        recorder.finish_trace(builder, status=200)
