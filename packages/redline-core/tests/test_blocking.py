"""Event-loop blocking detection: lag monitor, watchdog, sync calls, the detector."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import pytest

import redline_core.blocking as bl
from redline_core.blocking import call_name, note_sync_call, track_request
from redline_core.conf import Settings
from redline_core.detect import Detector
from redline_core.detectors import REGISTRY
from redline_core.detectors import blocking as blocking_detector
from redline_core.model import Callsite, Frame, Span, Trace, new_id
from redline_core.recorder import Recorder, record_span

ROOT = os.path.dirname(os.path.dirname(__file__))


def make_recorder(**settings) -> Recorder:
    return Recorder(Settings.from_mapping({"project_root": ROOT, **settings}))


async def run_request(recorder: Recorder, handler, path: str = "/x") -> Trace:
    builder = recorder.start_trace(method="GET", path=path, framework="fastapi")
    async with track_request(builder):
        await handler()
    return recorder.finish_trace(builder, status=200)


def blocking_spans(trace: Trace) -> list[Span]:
    return [s for s in trace.spans if s.kind == "blocking"]


def assert_idle() -> None:
    assert not bl._active
    assert not bl._loop_threads
    for monitor in list(bl._monitors.values()):
        assert monitor.handle is None and not monitor.trackers


# ── the lag monitor ───────────────────────────────────────────────────────────────────


async def test_time_sleep_in_async_handler_is_a_blocking_span_with_callsite():
    recorder = make_recorder()

    async def handler():
        await asyncio.sleep(0.01)
        time.sleep(0.12)  # the culprit
        await asyncio.sleep(0.01)

    trace = await run_request(recorder, handler)
    [span] = blocking_spans(trace)
    assert span.data["reason"] == "loop_lag"
    assert span.data["call"] == "time.sleep"
    assert 110 <= span.duration_ms <= 200
    assert span.data["lag_ms"] == span.duration_ms
    assert span.callsite is not None
    assert span.callsite.file == "tests/test_blocking.py"
    assert span.callsite.code == "time.sleep(0.12)  # the culprit"
    [issue] = [i for i in trace.issues if i.kind == "blocking_in_async"]
    assert issue.severity == "critical"  # ≥ 5 × 20 ms
    assert issue.title.startswith("Event loop blocked ")
    assert issue.title.endswith(f"ms by time.sleep at tests/test_blocking.py:{span.callsite.line}")
    assert issue.suggestion is not None and "asyncio.sleep" in issue.suggestion.summary
    assert issue.span_ids == [span.id]
    assert_idle()


async def test_stall_at_the_end_of_the_request_is_flushed_before_finishing():
    recorder = make_recorder()

    async def handler():
        time.sleep(0.06)  # no await afterwards: the monitor never gets the loop back

    trace = await run_request(recorder, handler)
    [span] = blocking_spans(trace)
    assert span.data["call"] == "time.sleep"
    assert span.duration_ms >= 55
    [issue] = [i for i in trace.issues if i.kind == "blocking_in_async"]
    assert issue.severity == "warning"
    assert_idle()


async def test_finish_inside_the_tracked_block_still_records_the_stall():
    """FastAPI finishes the trace from ``send`` – still inside ``track_request``."""
    recorder = make_recorder()
    builder = recorder.start_trace(method="GET", path="/x")
    async with track_request(builder):
        time.sleep(0.05)
        trace = recorder.finish_trace(builder, status=200)
    assert len(blocking_spans(trace)) == 1
    assert_idle()


async def test_short_blocks_and_awaits_are_not_reported():
    recorder = make_recorder()

    async def handler():
        for _ in range(3):
            time.sleep(0.002)
            await asyncio.sleep(0.01)

    trace = await run_request(recorder, handler)
    assert blocking_spans(trace) == []
    assert not [i for i in trace.issues if i.kind == "blocking_in_async"]


async def test_blocking_ms_setting_is_the_threshold():
    recorder = make_recorder(blocking_ms=200)

    async def handler():
        time.sleep(0.06)
        await asyncio.sleep(0)

    trace = await run_request(recorder, handler)
    assert blocking_spans(trace) == []


async def test_detect_blocking_off_starts_nothing():
    recorder = make_recorder(detect_blocking=False)
    builder = recorder.start_trace(method="GET", path="/x")
    async with track_request(builder) as tracker:
        assert tracker.monitor is None
        assert not bl._active
        time.sleep(0.04)
    trace = recorder.finish_trace(builder, status=200)
    assert blocking_spans(trace) == []


async def test_no_monitor_or_timer_without_a_request_in_flight():
    loop = asyncio.get_running_loop()
    assert_idle()
    before = len(loop._scheduled)  # type: ignore[attr-defined]
    recorder = make_recorder()
    builder = recorder.start_trace(method="GET", path="/x")
    async with track_request(builder) as tracker:
        assert tracker.monitor is not None and tracker.monitor in bl._active
        assert bl._loop_threads == {threading.get_ident(): 1}
        assert len(loop._scheduled) == before + 1  # type: ignore[attr-defined]
    recorder.finish_trace(builder, status=200)
    assert_idle()
    await asyncio.sleep(0.02)
    assert len([h for h in loop._scheduled if not h.cancelled()]) == before  # type: ignore[attr-defined]


async def test_concurrent_requests_share_one_monitor_and_only_the_culprit_gets_the_span():
    recorder = make_recorder()
    victim_started = asyncio.Event()

    async def culprit():
        await victim_started.wait()
        await asyncio.sleep(0.01)
        time.sleep(0.1)
        await asyncio.sleep(0.01)

    async def victim():
        victim_started.set()
        await asyncio.sleep(0.15)

    t_culprit, t_victim = await asyncio.gather(
        run_request(recorder, culprit, "/culprit"), run_request(recorder, victim, "/victim")
    )
    assert len(blocking_spans(t_culprit)) == 1
    assert blocking_spans(t_victim) == []
    assert len(bl._monitors) >= 1
    assert_idle()


async def test_inert_builder_and_ignored_paths_are_not_tracked():
    recorder = make_recorder()
    builder = recorder.start_trace(method="GET", path="/static/app.js")  # ignored path
    async with track_request(builder) as tracker:
        assert tracker.monitor is None
    async with track_request(None) as tracker:
        assert tracker.monitor is None


def test_track_request_without_a_running_loop_is_a_no_op():
    recorder = make_recorder()
    builder = recorder.start_trace(method="GET", path="/x")
    with track_request(builder) as tracker:
        assert tracker.monitor is None
    trace = recorder.finish_trace(builder, status=200)
    assert blocking_spans(trace) == []


def test_monitors_on_two_loops_in_two_threads():
    recorder = make_recorder()
    results: dict[str, Trace] = {}

    def worker(name: str, block: float) -> None:
        async def handler():
            await asyncio.sleep(0.01)
            time.sleep(block)
            await asyncio.sleep(0.01)

        results[name] = asyncio.run(run_request(recorder, handler, f"/{name}"))

    threads = [
        threading.Thread(target=worker, args=("a", 0.08)),
        threading.Thread(target=worker, args=("b", 0.0)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(blocking_spans(results["a"])) == 1
    assert blocking_spans(results["b"]) == []
    assert_idle()


# ── sync calls on the loop thread ────────────────────────────────────────────────────


async def test_note_sync_call_marks_slow_sync_driver_calls_on_the_loop():
    recorder = make_recorder()
    builder = recorder.start_trace(method="GET", path="/x")
    async with track_request(builder):
        start = time.perf_counter()
        sql = record_span("sql", "SELECT 1", start, 0.03, {"sql": "SELECT 1"})
        blocking = note_sync_call(sql, "sync ORM")
        fast = record_span("sql", "SELECT 2", start, 0.0001, {"sql": "SELECT 2"})
        assert note_sync_call(fast) is None  # under 1 ms
    trace = recorder.finish_trace(builder, status=200)
    assert blocking is not None
    [span] = blocking_spans(trace)
    assert span.data == {
        "reason": "sync_call",
        "call": "sync ORM",
        "lag_ms": sql.duration_ms,
        "span_id": sql.id,
    }
    assert span.start_ms == sql.start_ms and span.duration_ms == sql.duration_ms
    [issue] = [i for i in trace.issues if i.kind == "blocking_in_async"]
    assert "by sync ORM" in issue.title


async def test_note_sync_call_off_the_loop_thread_or_untracked_is_ignored():
    recorder = make_recorder()
    with recorder.recording(method="GET", path="/x"):  # not tracked
        span = record_span("sql", "SELECT 1", time.perf_counter(), 0.05, {"sql": "SELECT 1"})
        assert note_sync_call(span) is None
    builder = recorder.start_trace(method="GET", path="/y")
    async with track_request(builder):

        def in_thread():
            s = record_span("sql", "SELECT 1", time.perf_counter(), 0.05, {"sql": "SELECT 1"})
            return note_sync_call(s)

        import contextvars

        ctx = contextvars.copy_context()
        result = await asyncio.get_running_loop().run_in_executor(None, ctx.run, in_thread)
        assert result is None  # a worker thread does not block the loop
    recorder.finish_trace(builder, status=200)


async def test_loop_lag_overlapping_a_sync_call_is_not_recorded_twice():
    recorder = make_recorder()
    builder = recorder.start_trace(method="GET", path="/x")
    async with track_request(builder):
        await asyncio.sleep(0.01)
        start = time.perf_counter()
        time.sleep(0.06)
        span = record_span("sql", "SELECT 1", start, time.perf_counter() - start, {"sql": "x"})
        note_sync_call(span, "SQLAlchemy (sync)")
        await asyncio.sleep(0.01)
    trace = recorder.finish_trace(builder, status=200)
    assert [s.data["reason"] for s in blocking_spans(trace)] == ["sync_call"]


# ── naming the blocking call ─────────────────────────────────────────────────────────


def _frame_of(target, *args):
    """Run ``target`` in a thread and return its stack sampled mid-call."""
    ready = threading.Event()
    done = threading.Event()
    holder: dict[str, object] = {}

    def run():
        holder["ident"] = threading.get_ident()
        ready.set()
        target(*args, done)

    t = threading.Thread(target=run)
    t.start()
    ready.wait()
    time.sleep(0.02)
    frame = sys._current_frames()[holder["ident"]]  # type: ignore[index]
    capturer = make_recorder().capturer
    name = call_name(frame, capturer)
    del frame
    done.set()
    t.join()
    return name


def test_call_name_reads_the_project_line_for_c_functions():
    def sleeper(done):
        while not done.is_set():
            time.sleep(0.005)

    assert _frame_of(sleeper) == "time.sleep"


def test_call_name_names_library_apis_and_the_django_orm():
    ns: dict = {"__name__": "django.db.models.query"}
    exec("def fetch(done):\n    done.wait()\n", ns)  # a "library" frame (no project file)
    orm_fetch = ns["fetch"]

    def view(done):
        orm_fetch(done)

    assert _frame_of(view) == "sync ORM"

    ns2: dict = {"__name__": "requests.api"}
    exec("def get(done):\n    done.wait()\n", ns2)
    http_get = ns2["get"]

    def view2(done):
        http_get(done)

    assert _frame_of(view2) == "requests.get"


# ── the detector ─────────────────────────────────────────────────────────────────────


def _blocking(ms: float, call: str | None, line: int = 41, reason: str = "loop_lag") -> Span:
    cs = Callsite(
        "app/views.py", line, "view", "time.sleep(1)", (Frame("app/views.py", line, "v"),)
    )
    return Span(
        id=new_id(),
        parent_id=None,
        kind="blocking",
        name=call or "event loop blocked",
        start_ms=1.0,
        duration_ms=ms,
        callsite=cs,
        data={"reason": reason, "call": call, "lag_ms": ms},
    )


def _trace(*spans: Span, framework="fastapi") -> Trace:
    return Trace(
        id=new_id(),
        framework=framework,
        method="GET",
        path="/x",
        route="/x",
        view=None,
        status=200,
        started_at=0.0,
        duration_ms=500.0,
        spans=list(spans),
    )


def test_detector_registered():
    assert blocking_detector.detect in REGISTRY


def test_detector_thresholds_grouping_and_severity():
    settings = Settings()
    trace = _trace(
        _blocking(230, "time.sleep"),
        _blocking(30, "time.sleep"),
        _blocking(19, "time.sleep"),  # under blocking_ms
        _blocking(25, "requests.get", line=88),
    )
    issues = {i.extra["call"]: i for i in blocking_detector.detect(trace, settings)}
    sleep = issues["time.sleep"]
    assert sleep.title == "Event loop blocked 230 ms by time.sleep at app/views.py:41"
    assert sleep.severity == "critical" and sleep.count == 2 and sleep.total_ms == 260
    req = issues["requests.get"]
    assert req.severity == "warning"
    assert "httpx.AsyncClient" in req.suggestion.summary
    assert len(issues) == 2


def test_detector_issue_ids_are_stable_and_suggestions_survive_the_detector():
    a = Detector(Settings()).detect(_trace(_blocking(50, "sync ORM"), framework="django"))
    b = Detector(Settings()).detect(_trace(_blocking(80, "sync ORM"), framework="django"))
    [ia] = [i for i in a if i.kind == "blocking_in_async"]
    [ib] = [i for i in b if i.kind == "blocking_in_async"]
    assert ia.id == ib.id
    assert ia.suggestion is not None and "aget" in (ia.suggestion.code or "")


def test_detector_without_call_or_callsite():
    span = _blocking(40, None)
    span.callsite = None
    [issue] = blocking_detector.detect(_trace(span), Settings())
    assert issue.title == "Event loop blocked 40 ms"
    assert issue.suggestion.confidence == "low"


@pytest.mark.parametrize("framework,word", [("django", "sync_to_async"), ("fastapi", "threadpool")])
def test_detector_offload_advice_per_framework(framework, word):
    [issue] = blocking_detector.detect(
        _trace(_blocking(40, "app.heavy"), framework=framework), Settings()
    )
    assert word in issue.suggestion.summary


async def _sleepy():
    await asyncio.sleep(0.01)
    time.sleep(0.05)
    await asyncio.sleep(0.01)


async def test_watchdog_parks_after_the_last_request(monkeypatch):
    monkeypatch.setattr(bl, "WATCHDOG_LINGER", 0.05)
    recorder = make_recorder()
    await run_request(recorder, lambda: asyncio.sleep(0.01))
    assert bl._watchdog is not None and bl._watchdog.is_alive()
    for _ in range(50):
        if not bl._wake.is_set():
            break
        await asyncio.sleep(0.02)
    assert not bl._wake.is_set()  # parked on the event: no polling, no GIL traffic
    trace = await run_request(recorder, _sleepy)  # and it wakes up again
    assert blocking_spans(trace)[0].data["call"] == "time.sleep"
