"""v0.2 storage: schema migration, analytics tables, history, runs (CONTRACT §11)."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from redline_core.conf import Settings
from redline_core.detect import detect
from redline_core.histogram import Histogram
from redline_core.model import Span, new_id
from redline_core.runs import Run, RunStep
from redline_core.storage import ExtendedStore, FilterError, Filters, SQLiteStore, Store
from redline_core.storage.base import parse_period
from redline_core.storage.derive import fingerprint_key, pattern, url_parts
from redline_core.storage.sqlite import SCHEMA_VERSION, _bursts

from .conftest import make_callsite, make_trace, sql_span

HOUR = 3600
N1_SQL = 'SELECT * FROM "items" WHERE "order_id" = %s'


def http_span(url: str, status: int | None = 200, ms: float = 10.0, method: str = "GET") -> Span:
    return Span(
        new_id(),
        None,
        "http_out",
        f"{method} {url}",
        0.0,
        ms,
        None,
        {"method": method, "url": url, "status": status},
    )


def cache_span(op: str, key: str, hit: bool | None = None, backend: str = "default") -> Span:
    return Span(
        new_id(),
        None,
        "cache",
        f"{op} {key}",
        0.0,
        0.5,
        None,
        {"op": op, "key": key, "hit": hit, "backend": backend},
    )


def n1_trace(*, n: int = 6, ms: float = 1.0, **kw):
    cs = make_callsite()
    spans = [sql_span(N1_SQL, [i], callsite=cs, duration_ms=ms) for i in range(n)]
    t = make_trace(spans=spans, **kw)
    t.issues = detect(t)
    return t


def rows(store, sql, params=()):
    return [tuple(r) for r in store._query(sql, params)]


# patterns ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("user:42:profile", "user:*:profile"),
        ("/orders/42/items/7", "/orders/*/items/*"),
        ("/v2/users/550e8400-e29b-41d4-a716-446655440000", "/v2/users/*"),
        ("session:deadbeef0123", "session:*"),
        ("utf8:key", "utf8:key"),
        ("/static/app.css", "/static/app.css"),
    ],
)
def test_pattern(text, expected):
    assert pattern(text) == expected


def test_url_parts():
    assert url_parts("https://api.x.com:8443/u/42?token=[redacted]") == (
        "api.x.com:8443",
        "https://api.x.com:8443/u/*",
    )
    assert url_parts("", "GET api.x.com/u/9") == ("api.x.com", "api.x.com/u/*")


def test_bursts():
    assert _bursts([0, 0.2, 0.5], 3, 1.0) == 1
    assert _bursts([0, 0.6, 1.2, 1.8], 3, 1.0) == 0
    assert _bursts([0, 0.1, 0.2, 0.3, 0.4, 0.5], 3, 1.0) == 1  # one burst, counted once
    assert _bursts([0, 0.1, 0.2, 5, 5.1, 5.2], 3, 1.0) == 2
    assert _bursts([], 3, 1.0) == 0


def test_parse_period():
    assert parse_period("release:v1.2") == ("release", "v1.2")
    assert parse_period("range:100-200.5") == ("range", 100.0, 200.5)
    for bad in (None, "", "release:", "range:5-1", "range:a-b", "week:1", "v1"):
        with pytest.raises(FilterError):
            parse_period(bad)


def test_protocols(store):
    assert isinstance(store, Store) and isinstance(store, ExtendedStore)

    class Minimal:  # a v0.1 custom store is still a Store, just not an ExtendedStore
        path = ""

        def save(self, trace): ...
        def get(self, trace_id, *, include_profile=True): ...
        def get_profile(self, trace_id): ...
        def has_profile(self, trace_id): ...
        def list(self, filters=None, limit=50, cursor=None): ...
        def endpoints(self, window): ...
        def issues(self, window, kind=None): ...
        def delete(self, trace_id): ...
        def clear(self): ...
        def prune(self, max_age_s=None, max_rows=None): ...
        def count(self): ...
        def flush(self, timeout=None): ...
        def close(self): ...

    assert isinstance(Minimal(), Store) and not isinstance(Minimal(), ExtendedStore)


# migration ---------------------------------------------------------------------
_V01_SCHEMA = """
CREATE TABLE traces (id TEXT PRIMARY KEY, started_at REAL NOT NULL, framework TEXT NOT NULL,
    method TEXT NOT NULL, path TEXT NOT NULL, route TEXT, view TEXT, status INTEGER NOT NULL,
    duration_ms REAL NOT NULL, query_count INTEGER NOT NULL, query_ms REAL NOT NULL,
    issue_count INTEGER NOT NULL, issue_kinds TEXT NOT NULL, has_profile INTEGER NOT NULL,
    tags TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX traces_started ON traces (started_at DESC, id DESC);
CREATE INDEX traces_route ON traces (route, method);
CREATE TABLE profiles (trace_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE trace_tags (trace_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL);
CREATE TABLE issues (issue_id TEXT NOT NULL, trace_id TEXT NOT NULL, kind TEXT NOT NULL,
    started_at REAL NOT NULL, route TEXT NOT NULL, data TEXT NOT NULL);
"""


def _v01_db(path, traces):
    conn = sqlite3.connect(path)
    conn.executescript(_V01_SCHEMA)
    for t in traces:
        body = t.to_dict(include_profile=False)
        for key in ("logs", "logs_dropped", "release"):  # v0.1 bodies predate these
            body.pop(key)
        kinds = "," + ",".join(t.issue_kinds) + "," if t.issues else ""
        conn.execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.id,
                t.started_at,
                t.framework,
                t.method,
                t.path,
                t.route,
                t.view,
                t.status,
                t.duration_ms,
                t.query_count,
                t.query_ms,
                len(t.issues),
                kinds,
                0,
                json.dumps(t.tags),
                json.dumps(body),
            ),
        )
        for k, v in t.tags.items():
            conn.execute("INSERT INTO trace_tags VALUES (?, ?, ?)", (t.id, k, v))
        for i in t.issues:
            conn.execute(
                "INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?)",
                (i.id, t.id, i.kind, t.started_at, t.route, json.dumps(i.to_dict())),
            )
    conn.execute(
        "INSERT INTO traces (id, started_at, framework, method, path, status, "
        "duration_ms, query_count, query_ms, issue_count, issue_kinds, has_profile, "
        "tags, body) VALUES ('bad', 1, 'other', 'GET', '/', 200, 1, 0, 0, 0, '', 0, "
        "'{}', 'not json')"
    )
    conn.commit()
    conn.close()


def test_v01_database_upgrades_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    now = time.time()
    old = [n1_trace(started_at=now - 100), n1_trace(started_at=now - 50, tags={"a": "1"})]
    _v01_db(path, old)
    store = SQLiteStore(path, background=False, max_rows=None, max_age_s=None)
    try:
        assert rows(store, "SELECT value FROM meta WHERE key = 'schema_version'") == [
            (str(SCHEMA_VERSION),)
        ]
        assert store.count() == 3  # the corrupt row stays, it is only skipped by the backfill
        assert store.get(old[0].id) is not None
        assert store.list(Filters(tag=("a", "1"))).total == 1
        # backfilled from the stored bodies
        [sql] = store.sql_workload(3600)
        assert sql.count == 12 and sql.traces == 2
        assert sum(p["count"] for p in store.history(None, None, now - 7200, now).series) == 2
        assert store.issue_index()[0].occurrences == 2
        store.save(n1_trace(started_at=now))  # and new writes go to the new tables
        assert store.sql_workload(3600)[0].count == 18
    finally:
        store.close()
    # reopening does not migrate (or backfill) again
    store = SQLiteStore(path, background=False, max_rows=None, max_age_s=None)
    try:
        assert store.sql_workload(3600)[0].count == 18
        assert store.issue_index()[0].occurrences == 3
    finally:
        store.close()


def test_fresh_database_has_version(tmp_path):
    store = SQLiteStore(str(tmp_path / "new.db"), background=False)
    assert rows(store, "SELECT value FROM meta") == [(str(SCHEMA_VERSION),)]
    store.close()


# write path --------------------------------------------------------------------
def test_trace_tables_written_with_the_trace(store):
    t = n1_trace(ms=2.0)
    t.spans += [
        sql_span("UPDATE items SET qty = 1", duration_ms=5.0),
        http_span("https://api.x.com/u/1?x=1", 200, 10),
        http_span("https://api.x.com/u/2", 503, 30),
        http_span("https://api.x.com/u/3", None, 20),
        cache_span("get", "user:1", hit=True),
        cache_span("get", "user:2", hit=False),
        cache_span("set", "user:2"),
        cache_span("delete", "user:3"),
        cache_span("pipeline", ""),  # no key: ignored
    ]
    store.save(t)
    sql = rows(
        store,
        "SELECT fingerprint, sql, count, total_ms, max_ms, rows, rows_n, vendor "
        "FROM trace_sql ORDER BY count DESC",
    )
    fp = sql_span(N1_SQL).data["fingerprint"]
    assert sql[0] == (fingerprint_key(fp), fp, 6, 12.0, 2.0, 6, 6, "sqlite")
    assert sql[1][2:5] == (1, 5.0, 5.0)
    assert rows(
        store, "SELECT method, host, url_pattern, count, total_ms, max_ms, errors FROM trace_ext"
    ) == [("GET", "api.x.com", "https://api.x.com/u/*", 3, 60.0, 30.0, 2)]
    assert rows(
        store, "SELECT backend, key_pattern, gets, hits, misses, sets, deletes FROM trace_cache"
    ) == [("default", "user:*", 2, 1, 1, 1, 1)]


def test_background_writer_fills_tables(tmp_path):
    store = SQLiteStore(str(tmp_path / "bg.db"))
    for _ in range(5):
        store.save(n1_trace())
    assert store.flush(5)
    assert store.sql_workload(3600)[0].traces == 5
    store.close()


def test_resave_replaces_rows_without_double_counting_history(store):
    t = n1_trace()
    t.release = "v1"
    store.save(t)
    store.save(t)
    assert rows(store, "SELECT count(*) FROM trace_sql") == [(1,)]
    assert rows(store, "SELECT count FROM rollups") == [(1,)]
    assert store.releases()[0].requests == 1
    assert store.issue_index()[0].occurrences == 1


def test_rollups_merge_histograms(store):
    now = time.time()
    base = int(now // HOUR) * HOUR
    for i, ms in enumerate([10, 20, 30, 40, 1000]):
        store.save(make_trace(duration_ms=ms, started_at=base + i, status=500 if i == 0 else 200))
    store.save(make_trace(duration_ms=50, started_at=base + 1))  # second batch, same bucket
    [(count, errors, blob)] = rows(store, "SELECT count, errors, hist FROM rollups")
    hist = Histogram.from_bytes(blob)
    assert count == 6 and errors == 1 and hist.count == 6 and hist.max == 1000


def test_delete_prune_and_clear(store):
    now = time.time()
    t1 = n1_trace(started_at=now - 10)
    t1.spans.append(http_span("https://x.io/a"))
    t1.spans.append(cache_span("get", "k", True))
    t1.release = "r1"
    store.save(t1)
    store.delete(t1.id)
    for table in ("trace_sql", "trace_ext", "trace_cache", "trace_tags", "issues"):
        assert rows(store, f"SELECT count(*) FROM {table}") == [(0,)], table
    # the history survives the trace
    assert rows(store, "SELECT count(*) FROM rollups") == [(1,)]
    assert store.issue_index() and store.releases()

    old = n1_trace(started_at=now - 10 * 86_400)
    store.save(old)
    assert store.prune(max_age_s=86_400) == 1
    assert rows(store, "SELECT count(*) FROM trace_sql") == [(0,)]

    store.save(n1_trace(started_at=now))
    assert store.clear() == 1
    assert rows(store, "SELECT count(*) FROM trace_sql") == [(0,)]
    assert rows(store, "SELECT count(*) FROM rollups") != [(0,)]
    store.save_run(Run(name="r"))
    store.clear(history=True)
    for table in ("rollups", "releases", "issue_index", "issue_rollups", "runs"):
        assert rows(store, f"SELECT count(*) FROM {table}") == [(0,)], table


def test_history_retention(tmp_path):
    store = SQLiteStore(
        str(tmp_path / "h.db"), background=False, max_age_s=None, history_retention_s=10 * 86_400
    )
    now = time.time()
    old, new = n1_trace(started_at=now - 20 * 86_400), n1_trace(started_at=now)
    old.release, new.release = "old", "new"
    store.save(old)
    store.save(new)
    store.save_run(Run(name="done", created_at=now - 20 * 86_400, status="finished"))
    store.save_run(Run(name="live", created_at=now - 20 * 86_400, status="running"))
    store.save_run(Run(name="fresh", status="finished"))
    assert store.prune_history() > 0
    assert [r.release for r in store.releases()] == ["new"]
    assert rows(store, "SELECT count(*) FROM rollups") == [(1,)]
    assert rows(store, "SELECT count(*) FROM issue_rollups") == [(1,)]
    assert store.issue_index()[0].first_release == "old"  # entry still seen recently
    assert sorted(r.name for r in store.list_runs()) == ["fresh", "live"]
    with pytest.raises(ValueError):
        store.prune_history(-1)
    store.close()

    keep = SQLiteStore(str(tmp_path / "k.db"), background=False, history_retention_s=None)
    keep.save(old)
    assert keep.prune_history() == 0 and keep.prune_history(0) == 0
    assert keep.releases()
    keep.close()
    with pytest.raises(ValueError):
        SQLiteStore(":memory:", history_retention_s=-1)


def test_from_settings_history_retention(tmp_path):
    s = SQLiteStore.from_settings(
        Settings(storage_path=str(tmp_path / "s.db"), history_retention_days=3)
    )
    assert s.history_retention_s == 3 * 86_400
    s.close()
    s = SQLiteStore.from_settings(
        Settings(storage_path=str(tmp_path / "s.db"), history_retention_days=None)
    )
    assert s.history_retention_s is None
    s.close()


# filters -----------------------------------------------------------------------
def test_new_list_filters(store):
    now = time.time()
    a = n1_trace(started_at=now - 30)
    b = make_trace(started_at=now - 20, spans=[sql_span("SELECT 1 FROM users WHERE id = 5")])
    c = make_trace(started_at=now - 10, tags={"replay_of": a.id})
    for t in (a, b, c):
        store.save(t)
    ids = lambda f: [i["id"] for i in store.list(f).items]  # noqa: E731
    assert ids(Filters(sql='"ITEMS"')) == [a.id]
    assert ids(Filters(sql="users where")) == [b.id]
    assert ids(Filters(sql="5")) == []  # literals are normalised away
    assert ids(Filters(since=now - 25)) == [c.id, b.id]
    assert ids(Filters(replay_of=a.id.upper())) == [c.id]
    f = Filters.from_params({"sql": "items", "since": str(now - 25), "replay_of": "x"})
    assert (f.sql, f.since, f.replay_of) == ("items", now - 25, "x")
    with pytest.raises(FilterError):
        Filters.from_params({"since": "yesterday"})


def test_traces_since(store):
    now = time.time()
    t1 = make_trace(started_at=now - 3)
    t2 = make_trace(started_at=now - 2)
    t3 = make_trace(started_at=now - 2)
    for t in (t3, t1, t2):
        store.save(t)
    first, second = sorted([t2.id, t3.id])
    got = [s["id"] for s in store.traces_since(now - 5)]
    assert got == [t1.id, first, second]
    assert [s["id"] for s in store.traces_since(now - 2, first)] == [second]
    assert store.traces_since(now - 5, limit=1)[0]["id"] == t1.id
    assert set(store.traces_since(0)[0]) >= {"id", "issue_kinds", "has_profile", "tags"}


# analytics ---------------------------------------------------------------------
def test_search_sql_and_workload(store):
    now = time.time()
    store.save(n1_trace(n=6, ms=1.0, started_at=now - 10))
    store.save(n1_trace(n=4, ms=3.0, started_at=now - 5, route="/other/", method="POST"))
    store.save(make_trace(spans=[sql_span("SELECT 1 FROM users", duration_ms=0.5)]))
    store.save(n1_trace(started_at=now - 2 * 86_400))  # outside the window
    [items, users] = store.sql_workload(3600)
    assert items.count == 10 and items.traces == 2 and items.total_ms == 18.0
    assert items.max_ms == 3.0 and items.mean_ms == 1.8 and items.mean_rows == 1.0
    assert items.routes == ["GET /orders/<int:pk>/", "POST /other/"]
    assert items.vendor == "sqlite"
    d = items.to_dict()
    assert set(d) >= {
        "fingerprint",
        "sql",
        "count",
        "traces",
        "total_ms",
        "max_ms",
        "routes",
        "sample_trace_id",
    }
    assert store.get(d["sample_trace_id"]).route == "/other/"  # the latest occurrence
    assert [s.sql for s in store.search_sql("users", 3600)] == [users.sql]
    assert len(store.search_sql(None, 3600, limit=1)) == 1
    assert store.search_sql("nothing", 3600) == []
    assert store.search_sql("100%_", 3600) == []


def test_cache_stats(store):
    now = time.time()
    for i in range(3):  # three requests miss "user:*" within a second: a stampede
        store.save(
            make_trace(
                started_at=now - 10 + i * 0.2,
                spans=[cache_span("get", f"user:{i}", hit=False), cache_span("set", f"user:{i}")],
            )
        )
    store.save(make_trace(started_at=now - 5, spans=[cache_span("get", "user:9", hit=True)]))
    store.save(make_trace(spans=[cache_span("set", "report:1"), cache_span("set", "report:2")]))
    store.save(make_trace(spans=[cache_span("get", "x", backend="redis")]))
    stats = {(c.backend, c.key_pattern): c for c in store.cache_stats(3600)}
    user = stats[("default", "user:*")]
    assert (user.gets, user.hits, user.misses, user.sets) == (4, 1, 3, 3)
    assert user.hit_rate == 0.25 and user.unread_sets == 0 and user.stampedes == 1
    report = stats[("default", "report:*")]
    assert report.unread_sets == 2 and report.hit_rate is None
    assert stats[("redis", "x")].to_dict()["hit_rate"] is None


def test_external_stats(store):
    url = "https://pay.io/charge/{}"
    store.save(make_trace(spans=[http_span(url.format(1), 200, 10)]))
    store.save(
        make_trace(spans=[http_span(url.format(2), 200, 10), http_span(url.format(3), 200, 30)])
    )
    store.save(
        make_trace(
            spans=[
                http_span(url.format(4), 503, 100),
                http_span(url.format(4), 503, 100),
                http_span(url.format(4), 200, 100),
            ]
        )
    )
    store.save(make_trace(spans=[http_span("https://pay.io/charge/5", 200, 5, method="POST")]))
    by = {(e.method, e.url_pattern): e for e in store.external_stats(3600)}
    get = by[("GET", "https://pay.io/charge/*")]
    assert get.count == 6 and get.errors == 2 and get.max_ms == 100
    assert get.duplicates == 2 and get.retries == 1
    assert 99 <= get.p95_ms <= 101  # each call taken at its request's mean
    assert get.to_dict()["error_rate"] == round(2 / 6, 4)
    assert by[("POST", "https://pay.io/charge/*")].count == 1


# history -----------------------------------------------------------------------
def test_history_buckets_and_filters(store):
    day = 86_400
    start = (int(time.time()) // day - 3) * day  # midnight UTC, three days ago
    for h, ms, status in ((1, 10, 200), (1, 20, 500), (2, 30, 200), (day // HOUR + 1, 40, 200)):
        t = make_trace(started_at=start + h * HOUR + 5, duration_ms=ms, status=status)
        t.query_count = 2
        t.release = "v1" if h < 24 else "v2"
        store.save(t)
    store.save(make_trace(started_at=start + HOUR, route="/x/", method="POST", duration_ms=500))
    hourly = store.history("/orders/<int:pk>/", "GET", start, start + 2 * day)
    assert [p["t"] for p in hourly.series] == [start + HOUR, start + 2 * HOUR, start + 25 * HOUR]
    first = hourly.series[0]
    assert first["count"] == 2 and first["error_rate"] == 0.5 and first["avg_queries"] == 2
    assert 9.9 <= first["p50_ms"] <= 10.1 and 19.8 <= first["p99_ms"] <= 20.2
    daily = store.history("/orders/<int:pk>/", "GET", start, start + 2 * day, 86_400)
    assert [(p["t"], p["count"]) for p in daily.series] == [(start, 3), (start + day, 1)]
    assert [r["release"] for r in daily.releases] == ["v1", "v2"]
    everything = store.history(None, None, start, start + 2 * day, 86_400)
    assert everything.series[0]["count"] == 4
    assert store.history(None, "POST", start, start + day).series[0]["p95_ms"] == 500
    assert store.history(None, None, start + 3 * day, start + 4 * day).series == []
    assert set(hourly.to_dict()) == {"series", "releases"}


def test_releases(store):
    now = time.time()
    for rel, at in (("a", now - 100), ("a", now - 90), ("b", now - 10), (None, now)):
        t = make_trace(started_at=at)
        t.release = rel
        store.save(t)
    rel = store.releases()
    assert [(r.release, r.requests) for r in rel] == [("b", 1), ("a", 2)]
    assert rel[1].first_seen == now - 100 and rel[1].last_seen == now - 90
    assert set(rel[0].to_dict()) == {"release", "first_seen", "last_seen", "requests"}


def _release_traffic(store, release, at, *, route, ms, queries=1, issues=()):
    for _ in range(4):
        t = make_trace(started_at=at, route=route, path=route, duration_ms=ms)
        t.release = release
        t.query_count = queries
        t.issues = list(issues)
        store.save(t)


def test_compare_periods(store):
    now = time.time()
    n1 = n1_trace().issues[0]
    other = detect(make_trace(spans=[sql_span("SELECT 1 FROM t", [1]) for _ in range(3)]))[0]
    a, b = now - 3 * HOUR, now - HOUR
    _release_traffic(store, "v1", a, route="/slow/", ms=100, issues=[n1])
    _release_traffic(store, "v2", b, route="/slow/", ms=200, issues=[other])
    _release_traffic(store, "v1", a, route="/fast/", ms=100)
    _release_traffic(store, "v2", b, route="/fast/", ms=50)
    _release_traffic(store, "v1", a, route="/q/", ms=10, queries=1, issues=[n1])
    _release_traffic(store, "v2", b, route="/q/", ms=10, queries=3, issues=[n1])
    _release_traffic(store, "v1", a, route="/same/", ms=100)
    _release_traffic(store, "v2", b, route="/same/", ms=103)
    _release_traffic(store, "v1", a, route="/tiny/", ms=1)
    _release_traffic(store, "v2", b, route="/tiny/", ms=3)  # +200 % but < 5 ms
    _release_traffic(store, "v1", a, route="/gone/", ms=10)
    _release_traffic(store, "v2", b, route="/new/", ms=10)
    cmp = store.compare_periods(("release", "v1"), ("release", "v2"))
    verdicts = {e["route"]: e["verdict"] for e in cmp.endpoints}
    assert verdicts == {
        "/slow/": "regressed",
        "/fast/": "improved",
        "/q/": "regressed",
        "/same/": "same",
        "/tiny/": "same",
        "/gone/": "gone",
        "/new/": "new",
    }
    assert [e["verdict"] for e in cmp.endpoints][:2] == ["regressed", "regressed"]
    slow = next(e for e in cmp.endpoints if e["route"] == "/slow/")
    assert 99 <= slow["delta_p95_pct"] <= 101 and slow["delta_queries"] == 0
    assert slow["a"]["count"] == 4 and set(slow["a"]) >= {
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "error_rate",
        "avg_queries",
    }
    new = next(e for e in cmp.endpoints if e["route"] == "/new/")
    assert new["a"] is None and new["delta_p95_pct"] is None
    assert [i["id"] for i in cmp.issues["new"]] == [other.id]
    assert cmp.issues["resolved"] == []
    [persisting] = cmp.issues["persisting"]
    assert persisting["id"] == n1.id and persisting["occurrences"] == 4
    assert set(persisting) >= {"kind", "title", "severity", "route", "occurrences"}
    assert cmp.a == {"kind": "release", "release": "v1", "requests": 24}

    by_range = store.compare_periods(("range", a - 1, a + 1), ("range", b - 1, b + 1))
    assert {e["route"]: e["verdict"] for e in by_range.endpoints} == verdicts
    assert by_range.b["kind"] == "range" and by_range.b["requests"] == 24
    assert set(by_range.to_dict()) == {"a", "b", "endpoints", "issues"}
    with pytest.raises(ValueError):
        store.compare_periods(("week", "1"), ("release", "v1"))


def test_issue_index(store):
    now = time.time()
    for rel, at in (("v1", now - 100), ("v2", now - 50), ("v3", now - 10)):
        t = n1_trace(started_at=at)
        t.release = rel
        store.save(t)
    old = n1_trace(started_at=now - 5 * 86_400, path="/elsewhere/", route=None)
    old.issues[0].id = "old-issue"
    store.save(old)
    entries = store.issue_index()
    assert [e.id for e in entries][-1] == "old-issue"
    e = entries[0]
    assert (e.first_release, e.last_release, e.occurrences) == ("v1", "v3", 3)
    assert e.first_seen == now - 100 and e.last_seen == now - 10
    assert [x.id for x in store.issue_index(86_400)] == [e.id]
    assert set(e.to_dict()) >= {"id", "kind", "severity", "title", "route", "occurrences"}


# runs ----------------------------------------------------------------------------
def test_run_lifecycle(store):
    run = Run(name="checkout", config={"requests": [{"method": "GET", "path": "/"}]})
    run.steps.append(
        RunStep(step=0, target_rps=10, sent=100, ok=99, errors=1, hist=Histogram([5, 10, 15]))
    )
    store.save_run(run)
    step = RunStep(
        step=1,
        target_rps=20,
        started_at=1.0,
        sent=50,
        ok=40,
        errors=10,
        dropped=3,
        hist=Histogram([100] * 10),
        error_breakdown={"500": 10},
    )
    store.save_run_step(run.id, step)
    step.sent = 200  # a step is re-saved as it progresses
    step.ended_at = 31.0
    store.save_run_step(run.id, step)
    got = store.get_run(run.id)
    assert got.status == "running" and got.finished_at is None
    assert [s.step for s in got.steps] == [0, 1] and got.steps[1].sent == 200
    assert got.steps[1].hist == step.hist and got.steps[1].error_breakdown == {"500": 10}
    d = got.to_dict()
    assert set(d) == {
        "id",
        "name",
        "status",
        "config",
        "created_at",
        "finished_at",
        "steps",
        "summary",
    }
    assert d["summary"] == {"knee_step": None, "max_ok_rps": None, "verdict": None}
    s1 = d["steps"][1]
    assert set(s1) >= {
        "step",
        "target_rps",
        "achieved_rps",
        "sent",
        "ok",
        "errors",
        "dropped",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "max_ms",
        "error_rate",
        "error_breakdown",
    }
    assert s1["error_rate"] == 0.2 and s1["max_ms"] == 100 and s1["dropped"] == 3
    store.finish_run(run.id, "finished", {"knee_step": 1, "max_ok_rps": 10, "verdict": "knee"})
    got = store.get_run(run.id)
    assert got.status == "finished" and got.finished_at is not None
    assert got.to_dict()["summary"]["knee_step"] == 1
    later = Run(name="later", created_at=run.created_at + 1)
    store.save_run(later)
    assert [r.name for r in store.list_runs()] == ["later", "checkout"]
    assert [r.name for r in store.list_runs(limit=1)] == ["later"]
    assert len(store.list_runs()[1].steps) == 2
    assert store.delete_run(run.id) == 1 and store.delete_run(run.id) == 0
    assert store.get_run(run.id) is None
    assert rows(store, "SELECT count(*) FROM run_steps") == [(0,)]
    assert RunStep(step=0, target_rps=1).error_rate == 0.0


def test_run_bottlenecks_growth(store):
    run = Run(name="ramp")
    store.save_run(run)
    for step, rps in ((0, 10), (1, 20), (2, 40)):
        store.save_run_step(run.id, RunStep(step=step, target_rps=rps))
    # the query slows from 3 ms to 410 ms per execution as the rate steps up
    for step, query_ms, req_ms in ((0, 3.0, 20), (1, 30.0, 200), (2, 410.0, 2500)):
        for _ in range(2):
            t = n1_trace(
                n=2, ms=query_ms, duration_ms=req_ms, tags={"run_id": run.id, "step": str(step)}
            )
            store.save(t)
    other = make_trace(
        spans=[sql_span("SELECT 1 FROM t", duration_ms=1)], tags={"run_id": run.id, "step": "nope"}
    )
    store.save(other)  # an unparsable step is ignored
    store.save(n1_trace(tags={"run_id": "another", "step": "0"}))
    b = store.run_bottlenecks(run.id)
    assert b.steps == [
        {"step": 0, "target_rps": 10},
        {"step": 1, "target_rps": 20},
        {"step": 2, "target_rps": 40},
    ]
    sql, route = b.items
    assert sql["kind"] == "sql" and sql["key"].startswith("sql:")
    assert sql["label"] == 'SELECT * FROM "items" WHERE "order_id" = ?'
    assert [p["mean_ms"] for p in sql["per_step"]] == [3.0, 30.0, 410.0]
    assert [p["count"] for p in sql["per_step"]] == [4, 4, 4]
    assert sql["per_step"][2]["p95_ms"] == 410.0
    assert sql["growth"] == round(410 / 3, 2)
    assert sql["callsite"]["file"] == "shop/views.py"
    assert route["kind"] == "route" and route["label"] == "GET /orders/<int:pk>/"
    assert route["growth"] == 125.0 and route["callsite"] is None
    assert set(b.to_dict()) == {"steps", "items"}
    assert store.run_bottlenecks("missing") is None


def test_run_bottlenecks_edge_cases(store):
    empty = Run(name="empty")
    store.save_run(empty)
    assert store.run_bottlenecks(empty.id).to_dict() == {"steps": [], "items": []}
    # traces without a stored run: steps come from the tags; a zero first mean is guarded
    for step, ms in ((0, 0.0), (1, 5.0)):
        store.save(n1_trace(n=1, ms=ms, tags={"run_id": "cli", "step": str(step)}))
    b = store.run_bottlenecks("cli")
    assert b.steps == [{"step": 0, "target_rps": None}, {"step": 1, "target_rps": None}]
    sql = next(i for i in b.items if i["kind"] == "sql")
    assert sql["growth"] == 500.0
    store.save(n1_trace(tags={"run_id": "single", "step": "0"}))
    assert all(i["growth"] == 1.0 for i in store.run_bottlenecks("single").items)
