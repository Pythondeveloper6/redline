"""Work on threads and pools is attributed to the request that started it."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from redline_core import record_span, span
from redline_core.contrib import threads as integration
from redline_core.contrib.threads import instrument_threads, uninstrument_threads


@pytest.fixture(autouse=True)
def _restore():
    uninstrument_threads()
    yield
    uninstrument_threads()


def _work(label):
    record_span("custom", label, 0.0, 0.001, capture_callsite=False)
    return threading.current_thread().name


def test_without_the_integration_pool_work_is_not_attributed(tracer):
    builder = tracer.start()
    with ThreadPoolExecutor(2) as pool:
        pool.submit(_work, "lost").result()
    assert tracer.finish(builder) == []


def test_three_sql_queries_on_a_pool_appear_under_the_request(tracer, tmp_path):
    sa = pytest.importorskip("sqlalchemy")
    from redline_core.contrib.sqlalchemy import instrument_engine

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    instrument_engine(engine)
    barrier = threading.Barrier(3)  # all three run at once, on three different workers

    def query(i):
        barrier.wait(timeout=5)
        with engine.connect() as conn:
            return conn.execute(sa.text("SELECT :i"), {"i": i}).scalar()

    assert instrument_threads() and instrument_threads()
    builder = tracer.start()
    with span("view.handler", kind="view") as view:
        with ThreadPoolExecutor(3) as pool:
            assert sorted(pool.map(query, range(3))) == [0, 1, 2]
        view_id = view.span_id
    spans = tracer.finish(builder)
    engine.dispose()
    sql = [s for s in spans if s.kind == "sql"]
    assert len(sql) == 3
    assert {s.parent_id for s in sql} == {view_id}  # kept under the span that was open
    names = {s.data["thread"] for s in sql}
    assert len(names) == 3 and all(n.startswith("ThreadPoolExecutor") for n in names)


def test_thread_start_copies_context_and_labels_the_thread(tracer):
    assert instrument_threads()
    builder = tracer.start()
    t = threading.Thread(target=_work, args=("in-thread",), name="exporter-1")
    t.start()
    t.join()
    (recorded,) = tracer.finish(builder)
    assert recorded.name == "in-thread" and recorded.data["thread"] == "exporter-1"


def test_thread_subclass_with_run_override_is_covered(tracer):
    class Worker(threading.Thread):
        def run(self):
            _work("subclass")

    assert instrument_threads()
    builder = tracer.start()
    w = Worker(name="sub-1")
    w.start()
    w.join()
    (recorded,) = tracer.finish(builder)
    assert recorded.data["thread"] == "sub-1"


def test_run_in_executor_and_nested_submit(tracer):
    assert instrument_threads()

    async def main():
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(2) as pool:
            return await loop.run_in_executor(pool, _work, "from-loop")

    builder = tracer.start()
    asyncio.run(main())
    (recorded,) = tracer.finish(builder)
    assert recorded.name == "from-loop" and "thread" in recorded.data


def test_main_thread_spans_are_not_labelled_and_idle_calls_are_unwrapped(tracer):
    assert instrument_threads()
    builder = tracer.start()
    with span("local"):
        pass
    (local,) = tracer.finish(builder)
    assert "thread" not in local.data
    with ThreadPoolExecutor(1) as pool:  # no request: the original submit path
        assert pool.submit(_work, "nobody").result().startswith("ThreadPoolExecutor")


def test_pool_workers_do_not_keep_the_first_requests_context(tracer):
    assert instrument_threads()
    pool = ThreadPoolExecutor(1)
    try:
        first = tracer.start()
        pool.submit(_work, "one").result()
        tracer.finish(first)
        # the same worker, now with no request: it must not see the old builder
        seen = pool.submit(lambda: __import__("redline_core").current()).result()
        assert seen is None
    finally:
        pool.shutdown()


def test_threads_named_redline_are_left_alone(tracer):
    assert instrument_threads()
    seen = []
    builder = tracer.start()
    t = threading.Thread(target=lambda: seen.append(__import__("redline_core").current()))
    t.name = "redline-writer"
    t.start()
    t.join()
    tracer.finish(builder)
    assert seen == [None]


def test_work_that_outlives_the_request_is_dropped_not_misattributed(tracer):
    assert instrument_threads()
    gate, done = threading.Event(), threading.Event()

    def late():
        gate.wait(5)
        record_span("custom", "late", 0.0, 0.001, capture_callsite=False)
        done.set()

    builder = tracer.start()
    t = threading.Thread(target=late, name="late-1")
    t.start()
    spans = tracer.finish(builder)
    gate.set()
    assert done.wait(5)
    t.join()
    assert spans == []


def test_idempotent_and_reversible():
    original_submit = ThreadPoolExecutor.submit
    original_start = threading.Thread.start
    assert instrument_threads() and instrument_threads()
    assert ThreadPoolExecutor.submit is not original_submit
    assert threading.Thread.start is not original_start
    uninstrument_threads()
    uninstrument_threads()
    assert ThreadPoolExecutor.submit is original_submit
    assert threading.Thread.start is original_start
    assert not integration._originals


def test_concurrent_span_recording_is_thread_safe(tracer):
    assert instrument_threads()
    per_thread, workers = 60, 8  # 480 spans: under max_spans_per_trace
    builder = tracer.start()
    with ThreadPoolExecutor(workers) as pool:
        futures = [
            pool.submit(lambda: [_work("s") for _ in range(per_thread)]) for _ in range(workers)
        ]
        for f in futures:
            f.result()
    spans = tracer.finish(builder)
    assert len(spans) == per_thread * workers
    assert len({s.id for s in spans}) == len(spans)
