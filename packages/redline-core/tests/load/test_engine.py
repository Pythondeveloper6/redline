"""The engine end to end against a local server (spawns worker processes)."""

from __future__ import annotations

import re
import threading
from collections import Counter

import pytest

from redline_core.load import MemorySink, Profile, run

pytestmark = pytest.mark.slow


def scenario(url, **extra):
    return {
        "name": "t",
        "base_url": url,
        "requests": [{"path": "/a", "weight": 1}],
        "profile": {"mode": "constant", "rps": 20, "duration_s": 1},
        **extra,
    }


def test_steps_headers_and_aggregation(http_server):
    state, url = http_server
    sink = MemorySink()
    sc = scenario(
        url,
        requests=[
            {"path": "/a/{random_int:1:3}", "weight": 4},
            {"path": "/err", "weight": 1},
            {
                "method": "POST",
                "path": "/missing",
                "json": {"x": 1},
                "weight": 1,
                "expect_status": 404,
            },
        ],
        seed=3,
    )
    result = run(sc, profile=Profile.steps_of(15, 30, 15, step_duration_s=1), sink=sink, workers=2)
    assert result.status == "done"
    assert [s.step for s in result.steps] == [1, 2]
    assert [s.sent for s in result.steps] == [15, 30]
    assert all(s.dropped == 0 and not s.partial for s in result.steps)
    total = sum(s.sent for s in result.steps)
    assert len(state.headers) == total
    # every request carried X-Redline-Run with the step it was scheduled in
    pattern = re.compile(rf"^{result.run_id};step=(\d)$")
    per_step = Counter(pattern.match(h).group(1) for h in state.headers)
    assert per_step == {"1": 15, "2": 30}
    errors = sum(s.errors for s in result.steps)
    assert errors == state.paths.count("/err")
    assert sum(s.error_breakdown.get("500", 0) for s in result.steps) == errors
    assert all(re.fullmatch(r"/a/[123]|/err|/missing", p) for p in state.paths)
    assert sink.meta.id == result.run_id and sink.status == "done"
    assert [s.step for s in sink.steps] == [1, 2]
    assert sink.summary["sent"] == total
    # aligned steps: step 2 starts exactly where step 1 ends
    s1, s2 = result.steps
    assert s2.started_at == pytest.approx(s1.ended_at)
    assert s1.achieved_rps == pytest.approx(15, rel=0.01)


def test_coordinated_omission_is_corrected(serial_server):
    """One request stalls the (single-threaded) server for 1 s. Requests scheduled during
    the stall wait for the one connection: their latency counts from their *scheduled*
    time, so the stall shows up in the percentiles, while the service time stays small."""
    state, url = serial_server
    state.stall_on, state.stall_s = 6, 1.0
    sc = scenario(url, connections=1, profile={"rps": 20, "duration_s": 2})
    result = run(sc, sink=MemorySink(), workers=1)
    (st,) = result.steps
    assert st.sent == 40 and st.errors == 0
    # ~20 arrivals queued behind the stall: latencies spread from ~1 s down to ~0
    assert st.hist.percentile(90) > 500
    assert st.hist.max >= 900
    # an uncorrected tool (timing from the actual send) reports ~1 ms here
    assert st.hist.percentile(75) > 200
    assert st.service_hist.percentile(90) < 100  # only the stalled request itself is slow
    assert st.service_hist.max >= 900


def test_inflight_cap_counts_dropped(serial_server):
    state, url = serial_server
    state.stall_on, state.stall_s = 1, 1.2
    sc = scenario(url, connections=1, max_inflight=3, profile={"rps": 20, "duration_s": 1})
    (st,) = run(sc, workers=1).steps
    assert st.dropped > 10
    assert st.sent + st.dropped == 20


def test_stop_event_keeps_partial_results(http_server):
    _, url = http_server
    stop = threading.Event()
    sink = MemorySink()
    completed = []

    def on_event(kind, data):
        if kind == "step" and data.step == 1:
            # The callback runs after the sink retained a completed step. Worker
            # startup can take any amount of time without shortening this first step.
            assert data.sent == 10 and not data.partial
            completed.append(data)
            stop.set()

    result = run(
        scenario(url),
        profile=Profile.steps_of(10, 100, 10, step_duration_s=1),
        sink=sink,
        stop_event=stop,
        workers=2,
        on_event=on_event,
    )
    assert len(completed) == 1 and stop.is_set()
    assert result.status == "stopped" and sink.status == "stopped"
    assert result.steps[0].sent == 10 and not result.steps[0].partial
    assert result.steps[0] is completed[0]
    assert result.steps[-1].partial or len(result.steps) == 1
    assert result.summary["stopped_reason"] == "stop requested"


def test_stop_on_ends_the_run_early(http_server):
    _, url = http_server
    sc = scenario(
        url,
        requests=[{"path": "/err"}],
        profile={
            "mode": "steps",
            "start_rps": 10,
            "end_rps": 100,
            "step_rps": 10,
            "step_duration_s": 1,
            "stop_on": {"error_rate": 0.5},
        },
    )
    result = run(sc, workers=1)
    assert result.status == "done"
    assert "stop_on at step 1" in result.summary["stopped_reason"]
    assert len(result.steps) <= 3
    assert result.summary["verdict"] == "failing"


def test_worker_crash_fails_the_run(http_server):
    _, url = http_server
    sink = MemorySink()
    result = run(scenario(url, _crash_after_s=0.3), sink=sink, workers=2)
    assert result.status == "failed" and sink.status == "failed"
    assert "exited unexpectedly" in result.message
    assert result.summary["message"] == result.message


def test_connection_errors_are_classified():
    # nothing listens on port 9 (discard) on loopback in CI
    sc = scenario("http://127.0.0.1:9", timeout_s=1, profile={"rps": 5, "duration_s": 1})
    (st,) = run(sc, workers=1).steps
    assert st.errors == 5 and st.ok == 0
    assert set(st.error_breakdown) == {"ConnectError"}
