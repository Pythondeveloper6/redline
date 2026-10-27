from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from redline_core.detect import detect
from redline_core.storage import FilterError, Filters, SQLiteStore, Store, parse_window, percentile

from .conftest import make_callsite, make_trace, sql_span


def test_store_protocol_and_separate_wal_file(tmp_path):
    path = tmp_path / "sub" / "r.db"
    s = SQLiteStore(str(path), background=False)
    assert isinstance(s, Store)
    assert path.exists()
    mode = sqlite3.connect(str(path)).execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    s.close()
    s.close()  # idempotent


def test_save_get_profile_delete(store):
    t = make_trace(profile={"shared": {"frames": []}, "profiles": []})
    store.save(t)
    got = store.get(t.id)
    assert got == t
    light = store.get(t.id, include_profile=False)
    assert light.profile is None and store.has_profile(t.id)
    assert store.get_profile(t.id) == t.profile
    assert store.get("nope") is None and store.get_profile("nope") is None
    assert store.delete(t.id) == 1 and store.delete(t.id) == 0
    assert store.get_profile(t.id) is None and store.count() == 0


def test_resave_replaces(store):
    t = make_trace(tags={"a": "1"})
    store.save(t)
    t.tags = {"a": "2"}
    store.save(t)
    assert store.count() == 1
    assert store.list(Filters(tag=("a", "1"))).total == 0
    assert store.list(Filters(tag=("a", "2"))).total == 1


def _seed(store, now):
    from redline_core.runs import Run

    for run_id in ("r1", "r2"):  # run_id tags are kept only for runs the store knows
        store.save_run(Run(id=run_id))
    cs = make_callsite()
    n1 = [
        sql_span('SELECT * FROM "items" WHERE "order_id" = %s', [i], callsite=cs) for i in range(6)
    ]
    traces = [
        make_trace(path="/orders/1/", status=200, duration_ms=10, started_at=now - 50, spans=n1),
        make_trace(path="/orders/2/", status=404, duration_ms=20, started_at=now - 40),
        make_trace(
            path="/cart/",
            route="/cart/",
            method="POST",
            status=500,
            duration_ms=300,
            started_at=now - 30,
            tags={"run_id": "r1"},
        ),
        make_trace(
            path="/cart/",
            route="/cart/",
            method="POST",
            status=201,
            duration_ms=30,
            started_at=now - 20,
            tags={"run_id": "r2"},
        ),
        make_trace(path="/search", route=None, status=302, duration_ms=5, started_at=now - 10),
    ]
    for t in traces:
        t.issues = detect(t)
        store.save(t)
    return traces


@pytest.mark.parametrize(
    ("params", "paths"),
    [
        ({}, ["/search", "/cart/", "/cart/", "/orders/2/", "/orders/1/"]),
        ({"q": "/ORDERS"}, ["/orders/2/", "/orders/1/"]),
        ({"q": "views.order"}, ["/search", "/cart/", "/cart/", "/orders/2/", "/orders/1/"]),
        ({"method": "post"}, ["/cart/", "/cart/"]),
        ({"status": "5xx"}, ["/cart/"]),
        ({"status": "4xx"}, ["/orders/2/"]),
        ({"status": "2xx"}, ["/cart/", "/orders/1/"]),
        ({"status": "302"}, ["/search"]),
        ({"min_ms": "25"}, ["/cart/", "/cart/"]),
        ({"min_queries": "6"}, ["/orders/1/"]),
        ({"has_issues": "1"}, ["/orders/1/"]),
        ({"has_issues": "0"}, ["/search", "/cart/", "/cart/", "/orders/2/", "/orders/1/"]),
        ({"route": "/cart/"}, ["/cart/", "/cart/"]),
        ({"tag": "run_id:r1"}, ["/cart/"]),
        ({"q": "%"}, []),
        ({"q": "o_d"}, []),
        ({"q": "r_d"}, ["/search", "/cart/", "/cart/", "/orders/2/", "/orders/1/"]),
        ({"method": "POST", "status": "2xx"}, ["/cart/"]),
    ],
)
def test_list_filters(store, params, paths):
    _seed(store, time.time())
    page = store.list(Filters.from_params(params))
    assert [i["path"] for i in page.items] == paths
    assert page.total == len(paths)


@pytest.mark.parametrize(
    "params",
    [
        {"status": "6xx"},
        {"status": "abc"},
        {"min_ms": "x"},
        {"min_queries": "1.5"},
        {"tag": "nocolon"},
    ],
)
def test_bad_filters(params):
    with pytest.raises(FilterError):
        Filters.from_params(params)


def test_summary_shape(store):
    traces = _seed(store, time.time())
    item = store.list(Filters(has_issues=True)).items[0]
    assert item["id"] == traces[0].id
    assert item["issue_kinds"] == ["n_plus_one"] and item["issue_count"] == 1
    assert item["has_profile"] is False and item["query_count"] == 6
    assert item == traces[0].summary()


def test_cursor_pagination_is_stable_with_equal_timestamps(store):
    now = time.time()
    ids = set()
    for _ in range(7):
        t = make_trace(started_at=now)
        ids.add(t.id)
        store.save(t)
    for _ in range(5):
        store.save(make_trace(started_at=now - 1))
    seen, cursor, pages = [], None, 0
    while True:
        page = store.list(limit=3, cursor=cursor)
        assert page.total == 12
        seen += [i["id"] for i in page.items]
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 12 and pages == 4
    assert ids == set(seen[:7])
    with pytest.raises(FilterError):
        store.list(cursor="!!!not-a-cursor")


def test_endpoints_aggregates(store):
    now = time.time()
    for i, d in enumerate(range(1, 101)):
        store.save(
            make_trace(
                path=f"/o/{i}/",
                route="/o/<pk>/",
                duration_ms=float(d),
                status=500 if i < 10 else 200,
                started_at=now - 3000 + i * 10,
                spans=[sql_span("SELECT 1", [])] * (i % 3),
            )
        )
    store.save(make_trace(path="/old", route="/old", started_at=now - 7200))
    store.save(make_trace(path="/x", route=None, started_at=now - 5))
    stats = {(e.route, e.method): e for e in store.endpoints(3600, now=now)}
    assert ("/old", "GET") not in stats
    assert ("/x", "GET") in stats  # unresolved route falls back to the path
    e = stats[("/o/<pk>/", "GET")]
    assert e.count == 100
    assert e.error_rate == 0.1
    assert e.p50_ms == pytest.approx(50.5)
    assert e.p95_ms == pytest.approx(95.05)
    assert e.p99_ms == pytest.approx(99.01)
    assert e.max_queries == 2 and e.avg_queries == pytest.approx(0.99)
    assert 1 <= len(e.series) <= 48
    assert sum(b["count"] for b in e.series) == 100
    assert all(now - 3600 <= b["t"] <= now for b in e.series)
    assert set(e.to_dict()) >= {
        "route",
        "method",
        "count",
        "error_rate",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "avg_queries",
        "max_queries",
        "avg_query_ms",
        "issue_count",
        "series",
    }


def test_issue_groups_dedupe_across_traces(store):
    now = time.time()
    cs = make_callsite()
    for k, route in enumerate(["/a/", "/b/", "/a/"]):
        spans = [
            sql_span('SELECT * FROM "items" WHERE "order_id" = %s', [i], callsite=cs)
            for i in range(5 + k)
        ]
        t = make_trace(route=route, started_at=now - 100 + k, spans=spans)
        t.issues = detect(t)
        store.save(t)
        last = t
    slow = make_trace(
        started_at=now - 5, spans=[sql_span("SELECT * FROM big", [], duration_ms=700)]
    )
    slow.issues = detect(slow)
    store.save(slow)
    groups = store.issues(3600, now=now)
    assert [g.kind for g in groups] == ["slow_query", "n_plus_one"]  # critical first
    g = groups[1]
    assert g.occurrences == 3 and g.routes == ["/a/", "/b/"]
    assert g.first_seen == pytest.approx(now - 100) and g.last_seen == pytest.approx(now - 98)
    assert g.sample_trace_id == last.id and len(g.sample_span_ids) == 7
    assert g.callsite.location == "shop/views.py:41" and g.suggestion is not None
    assert [x.kind for x in store.issues(3600, kind="n_plus_one", now=now)] == ["n_plus_one"]
    assert store.issues(10, now=now - 200) == []


def test_prune_by_age_and_rows(store):
    now = time.time()
    old = make_trace(started_at=now - 10 * 86400, profile={"p": 1})
    store.save(old)
    fresh = [make_trace(started_at=now - i) for i in range(5)]
    for t in fresh:
        store.save(t)
    assert store.prune(max_age_s=86400, max_rows=3) == 3
    left = {i["id"] for i in store.list().items}
    assert left == {t.id for t in fresh[:3]}
    assert store.get_profile(old.id) is None
    assert store.clear() == 3 and store.count() == 0


def test_background_writer_prunes_automatically(tmp_path):
    s = SQLiteStore(str(tmp_path / "r.db"), max_rows=5, prune_every=10)
    for i in range(30):
        s.save(make_trace(started_at=time.time() + i))
    assert s.flush(5)
    assert s.count() <= 15  # pruned at least once while writing
    s.prune()
    assert s.count() == 5
    s.close()


def test_background_writer_flush_and_close(tmp_path):
    s = SQLiteStore(str(tmp_path / "r.db"))
    for _ in range(120):
        s.save(make_trace())
    assert s.flush(5)
    assert s.count() == 120 and s.written == 120
    s.close()
    s.save(make_trace())  # after close: dropped, never raises
    assert s.dropped == 1
    assert s.flush() is True


def test_full_queue_drops_and_never_blocks(tmp_path):
    s = SQLiteStore(str(tmp_path / "r.db"), queue_size=5, batch_size=1)
    gate = threading.Event()
    original = s._write

    def slow_write(batch):
        gate.wait(5)
        original(batch)

    s._write = slow_write  # type: ignore[method-assign]
    t0 = time.perf_counter()
    for _ in range(50):
        s.save(make_trace())
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5  # never waited on the blocked writer
    assert s.dropped >= 50 - 5 - 1
    gate.set()
    assert s.flush(5)
    assert s.count() == 50 - s.dropped
    s.close()


def test_concurrent_saves_and_reads(tmp_path):
    s = SQLiteStore(str(tmp_path / "r.db"))

    def writer():
        for _ in range(50):
            s.save(make_trace())

    def reader():
        for _ in range(20):
            s.list(limit=10)
            s.endpoints(3600)

    threads = [threading.Thread(target=writer) for _ in range(4)] + [
        threading.Thread(target=reader) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert s.flush(5)
    assert s.count() == 200
    s.close()


def test_memory_store():
    s = SQLiteStore(":memory:")
    s.save(make_trace())
    assert s.flush()
    assert s.count() == 1
    s.close()


def test_from_settings(tmp_path):
    from redline_core.conf import Settings

    s = SQLiteStore.from_settings(
        Settings(storage_path=str(tmp_path / "x.db"), max_rows=3), background=False
    )
    assert s.max_rows == 3 and s.path.endswith("x.db")
    s.close()


def test_helpers():
    assert (
        parse_window("1h") == 3600 and parse_window(None) == 86400 and parse_window("30m") == 1800
    )
    with pytest.raises(FilterError):
        parse_window("forever")
    assert percentile([], 50) == 0 and percentile([5], 99) == 5
    assert percentile([1, 2, 3, 4], 50) == 2.5
