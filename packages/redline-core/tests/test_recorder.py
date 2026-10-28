from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import redline_core
from redline_core.callsite import CallsiteCapturer
from redline_core.conf import Settings
from redline_core.recorder import Recorder, current, record_query, record_span, span


def make_recorder(store=None, **settings) -> Recorder:
    import os

    return Recorder(
        Settings.from_mapping({"project_root": os.path.dirname(__file__), **settings}), store
    )


def query(sql="SELECT * FROM t WHERE id = %s", params=(1,)):
    t0 = time.perf_counter()
    return record_query(sql, list(params), start=t0, duration=0.0005, vendor="sqlite")


# ── inactive ──────────────────────────────────────────────────────────────────


def test_everything_is_a_noop_without_a_trace():
    assert current() is None
    assert record_span("custom", "x", time.perf_counter(), 0.0) is None
    assert query() is None
    with span("block") as s:
        assert s.span_id is None

    @span
    def f(x):
        return x * 2

    assert f(2) == 4


async def test_async_noop():
    async with span("x"):
        pass

    @span("named")
    async def g():
        return 1

    assert await g() == 1


# ── basic recording ───────────────────────────────────────────────────────────


def test_records_queries_spans_and_callsites():
    rec = make_recorder()
    b = rec.start_trace(method="get", path="/orders/1/", framework="django")
    assert current() is b
    with span("outer", kind="view", user=3) as outer:
        query()
        with span("inner"):
            query("SELECT * FROM u WHERE x IN (1,2)", ())
    trace = rec.finish_trace(b, status=200, route="/orders/<int:pk>/", view="v")
    assert current() is None
    assert trace.method == "GET" and trace.route == "/orders/<int:pk>/"
    assert trace.query_count == 2 and trace.query_ms > 0
    by_name = {s.name: s for s in trace.spans}
    assert by_name["outer"].parent_id is None and by_name["outer"].kind == "view"
    assert by_name["outer"].data == {"user": 3}
    assert (
        by_name["inner"].parent_id == outer.span_id
        or by_name["inner"].parent_id == by_name["outer"].id
    )
    sqls = trace.sql_spans()
    assert sqls[0].parent_id == by_name["outer"].id
    assert sqls[1].parent_id == by_name["inner"].id
    assert sqls[1].data["fingerprint"] == "SELECT * FROM u WHERE x IN (...)"
    assert sqls[0].data["explainable"] is True and sqls[0].data["vendor"] == "sqlite"
    cs = sqls[0].callsite
    assert cs is not None and cs.file == "test_recorder.py" and cs.function == "query"
    assert cs.stack[-1] == cs.stack[-1] and len(cs.stack) >= 2
    assert all(s.start_ms >= 0 for s in trace.spans)
    assert trace.tags["sampled_by"] == "all"


def test_span_records_exception_and_reraises():
    rec = make_recorder()
    b = rec.start_trace(method="GET", path="/")
    with pytest.raises(ValueError), span("boom"):
        raise ValueError("x")
    trace = rec.finish_trace(b, status=500, error=ValueError("x"))
    assert trace.spans[0].data["error"] == "ValueError"
    assert trace.error["type"] == "ValueError" and "ValueError" in trace.error["traceback"]


def test_decorator_forms():
    rec = make_recorder()

    @span
    def bare():
        return "a"

    @span()
    def empty():
        return "b"

    @span("custom-name", kind="template", tpl="x.html")
    def named():
        return "c"

    b = rec.start_trace(method="GET", path="/")
    assert (bare(), empty(), named()) == ("a", "b", "c")
    trace = rec.finish_trace(b, status=200)
    names = [s.name for s in trace.spans]
    assert names[0].endswith("bare") and names[1].endswith("empty") and names[2] == "custom-name"
    assert trace.spans[2].kind == "template" and trace.spans[2].data == {"tpl": "x.html"}
    assert bare.__name__ == "bare"


async def test_async_decorator_and_nested_tasks():
    rec = make_recorder()

    @span("child")
    async def child(i):
        await asyncio.sleep(0)
        query(params=(i,))
        return i

    b = rec.start_trace(method="GET", path="/a")
    async with span("parent"):
        results = await asyncio.gather(*(child(i) for i in range(3)))
    trace = rec.finish_trace(b, status=200)
    assert results == [0, 1, 2]
    parent = next(s for s in trace.spans if s.name == "parent")
    children = [s for s in trace.spans if s.name == "child"]
    assert len(children) == 3 and all(c.parent_id == parent.id for c in children)
    child_ids = {c.id for c in children}
    assert all(s.parent_id in child_ids for s in trace.sql_spans())
    assert trace.query_count == 3


# ── isolation ─────────────────────────────────────────────────────────────────


async def test_concurrent_asyncio_requests_are_isolated():
    rec = make_recorder()

    async def request(n: int):
        b = rec.start_trace(method="GET", path=f"/r/{n}")
        for i in range(n):
            await asyncio.sleep(0.001 * ((n + i) % 3))
            query(params=(n,))
        async with span(f"s{n}"):
            await asyncio.sleep(0)
        return rec.finish_trace(b, status=200)

    traces = await asyncio.gather(*(request(n) for n in range(1, 21)))
    for n, trace in enumerate(traces, start=1):
        assert trace.path == f"/r/{n}"
        assert trace.query_count == n
        assert all(s.data["params"] == [n] for s in trace.sql_spans())
        assert [s.name for s in trace.spans if s.kind == "custom"] == [f"s{n}"]
    assert current() is None


def test_threads_with_copied_context_record_into_the_request():
    rec = make_recorder()
    b = rec.start_trace(method="GET", path="/")
    with ThreadPoolExecutor(4) as pool:
        futures = [pool.submit(contextvars.copy_context().run, query) for _ in range(8)]
        for f in futures:
            f.result()
    # a plain thread does not copy context: its work is not attributed
    t = threading.Thread(target=query)
    t.start()
    t.join()
    trace = rec.finish_trace(b, status=200)
    assert trace.query_count == 8


def test_concurrent_threads_each_with_own_trace():
    rec = make_recorder()
    results = {}

    def worker(n):
        b = rec.start_trace(method="GET", path=f"/t/{n}")
        for _ in range(n):
            query(params=(n,))
            time.sleep(0)
        results[n] = rec.finish_trace(b, status=200)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 17)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for n, trace in results.items():
        assert trace.query_count == n
        assert {tuple(s.data["params"]) for s in trace.sql_spans()} == {(n,)}


def test_finish_in_other_context_does_not_leak():
    rec = make_recorder()
    b = rec.start_trace(method="GET", path="/")
    ctx = contextvars.copy_context()
    ctx.run(rec.finish_trace, b, status=200)  # token belongs to the outer context
    assert ctx.run(current) is None
    # the outer context still holds the builder, but a finished builder is ignored
    assert current() is None
    assert query() is None
    with span("late") as s:
        assert s.span_id is None


# ── sampling, ignore, redaction, detection, storage ───────────────────────────


def test_ignored_paths_are_inert(store):
    rec = make_recorder(store, ignore_paths=["/static/", "^/health$"])
    for path in ("/static/app.js", "/health"):
        b = rec.start_trace(method="GET", path=path)
        assert current() is None
        query()
        trace = rec.finish_trace(b, status=200)
        assert trace.spans == [] and "sampled_by" not in trace.tags
    assert store.count() == 0
    assert rec.should_record("/healthz")


def test_disabled_records_nothing(store):
    rec = make_recorder(store, enabled=False)
    b = rec.start_trace(method="GET", path="/")
    assert current() is None
    rec.finish_trace(b, status=200)
    assert store.count() == 0


def test_sampling_keeps_slow_and_errors_only(store):
    import os

    rec = Recorder(
        Settings(sample_rate=0.0, always_keep_slower_than_ms=50, project_root=os.getcwd()),
        store,
    )
    kept = []
    for status, sleep in ((200, 0), (500, 0), (200, 0.06)):
        b = rec.start_trace(method="GET", path="/")
        time.sleep(sleep)
        kept.append(rec.finish_trace(b, status=status).tags.get("sampled_by"))
    assert kept == [None, "error", "slow"]
    assert store.count() == 2


def test_redaction_detection_and_storage(store):
    rec = make_recorder(store, n_plus_one_threshold=3)
    b = rec.start_trace(
        method="POST",
        path="/login",
        headers=[(b"authorization", b"Bearer s3cret"), (b"content-type", b"application/json")],
        query="token=abc&x=1",
        body=b'{"password": "hunter2", "user": "bob"}',
        client="1.2.3.4",
    )
    for i in range(4):
        query(params=(i,))
    trace = rec.finish_trace(
        b, status=200, headers={"Set-Cookie": "sid=1", "Content-Type": "text/plain"}, body="ok"
    )
    assert trace.request["headers"]["authorization"] == "[redacted]"
    assert trace.request["query"] == {"token": "[redacted]", "x": "1"}
    assert "hunter2" not in trace.request["body"] and "bob" in trace.request["body"]
    assert trace.response == {
        "headers": {"Set-Cookie": "[redacted]", "Content-Type": "text/plain"},
        "body": "ok",
        "body_truncated": False,
        "size": 2,
    }
    assert [i.kind for i in trace.issues] == ["n_plus_one"]
    stored = store.get(trace.id)
    assert stored is not None and stored.issues[0].count == 4
    assert "s3cret" not in str(stored.to_dict())


def test_body_capture_can_be_disabled():
    rec = make_recorder(capture_request_body=False, capture_response_body=False)
    b = rec.start_trace(method="POST", path="/", body=b"hello")
    t = rec.finish_trace(b, status=200, body=b"world")
    assert t.request["body"] is None and t.response["body"] is None and t.response["size"] == 5


def test_run_header_tags():
    rec = make_recorder()
    b = rec.start_trace(method="GET", path="/", headers={"X-Redline-Run": "abc123;step=4"})
    t = rec.finish_trace(b, status=200)
    assert t.tags["run_id"] == "abc123" and t.tags["step"] == "4"


def test_record_span_fills_sql_metadata_and_executemany():
    rec = make_recorder()
    b = rec.start_trace(method="GET", path="/")
    s = record_span("sql", "x", time.perf_counter(), 0.001, {"sql": "SELECT 1"})
    m = record_query(
        "INSERT INTO t (password) VALUES (%s)",
        [("a",), ("b",)] * 10,
        start=time.perf_counter(),
        duration=0.001,
        many=True,
    )
    rec.finish_trace(b, status=200)
    assert s.data["fingerprint"] == "SELECT ?" and s.data["explainable"] is True
    assert m.data["many"] is True and m.data["explainable"] is False
    assert len(m.data["params"]) == 10 and m.data["params"][0] == ["[redacted]"]
    assert m.data["vendor"] == "other"


def test_recording_context_manager():
    rec = make_recorder()
    with rec.recording(method="GET", path="/x"):
        query()
    assert rec.last_trace.status == 200 and rec.last_trace.query_count == 1
    with pytest.raises(KeyError), rec.recording(method="GET", path="/y"):
        raise KeyError("k")
    assert rec.last_trace.status == 500 and rec.last_trace.error["type"] == "KeyError"


def test_module_level_api_and_export():
    assert redline_core.span is span
    rec = redline_core.configure(Settings(project_root="/nonexistent"))
    assert redline_core.get_recorder() is rec
    b = redline_core.start_trace(method="GET", path="/")
    query()
    t = redline_core.finish_trace(b, status=204)
    assert t.status == 204 and t.sql_spans()[0].callsite is None  # outside project root


def test_capture_callsite_off():
    rec = make_recorder()
    b = rec.start_trace(method="GET", path="/")
    record_query("SELECT 1", start=time.perf_counter(), duration=0, capture_callsite=False)
    t = rec.finish_trace(b, status=200)
    assert t.spans[0].callsite is None


def test_profiling_opt_in_produces_speedscope():
    pytest.importorskip("pyinstrument")
    rec = make_recorder()
    b = rec.start_trace(method="GET", path="/", profile_requested=True)
    end = time.perf_counter() + 0.02
    while time.perf_counter() < end:
        sum(range(100))
    t = rec.finish_trace(b, status=200)
    assert t.profile is not None and "profiles" in t.profile and "shared" in t.profile


def test_requested_profile_is_kept_even_without_samples(monkeypatch):
    # a very fast request can end before the profiler's first sample; it was still
    # explicitly profiled, so it must be kept (this used to depend on machine speed)
    pytest.importorskip("pyinstrument")
    from redline_core.profiler import RequestProfiler

    real_stop = RequestProfiler.stop

    def no_samples(self, name=None, *, trim=True):
        real_stop(self, name, trim=trim)  # really stop pyinstrument, then report nothing
        return None

    monkeypatch.setattr(RequestProfiler, "stop", no_samples)
    rec = make_recorder(sample_rate=0.0, always_keep_slower_than_ms=60_000)
    b = rec.start_trace(method="GET", path="/", profile_requested=True)
    t = rec.finish_trace(b, status=200)
    assert t.profile is None and t.tags.get("sampled_by") == "profile"


async def test_profiling_concurrent_async_requests():
    pytest.importorskip("pyinstrument")
    rec = make_recorder(profile="all")

    async def req(n):
        b = rec.start_trace(method="GET", path=f"/{n}")
        end = time.perf_counter() + 0.01
        while time.perf_counter() < end:
            pass
        await asyncio.sleep(0.005)
        return rec.finish_trace(b, status=200)

    traces = await asyncio.gather(*(req(n) for n in range(3)))
    assert all(t.profile is not None for t in traces)
    assert all("profile_error" not in t.tags for t in traces)


def test_profile_off_never_profiles():
    rec = make_recorder(profile="off")
    b = rec.start_trace(method="GET", path="/", profile_requested=True)
    assert b.profiler is None
    rec.finish_trace(b, status=200)


def test_custom_capturer_is_used():
    rec = Recorder(Settings(), capturer=CallsiteCapturer("/nonexistent-root"))
    b = rec.start_trace(method="GET", path="/")
    query()
    assert rec.finish_trace(b, status=200).spans[0].callsite is None
