"""Refactor check: schema v4, store methods, X-Redline-Check tagging and keep rule, sinks."""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from redline_core.conf import Settings
from redline_core.detect import detect
from redline_core.recorder import Recorder
from redline_core.redact import MASK
from redline_core.storage import SQLiteStore
from redline_core.storage.checks import DEAD_CHECK_MESSAGE
from redline_core.storage.sqlite import SCHEMA_VERSION

from .conftest import make_callsite, make_trace, sql_span

N1_SQL = 'SELECT * FROM "items" WHERE "order_id" = %s'


def rows(store, sql, params=()):
    return [tuple(r) for r in store._query(sql, params)]


def tables(store):
    return {r[0] for r in rows(store, "SELECT name FROM sqlite_master WHERE type = 'table'")}


def new_run(store, run_id="c1", **kw):
    run = {
        "id": run_id,
        "suite_id": "s1",
        "kind": "baseline",
        "baseline_id": None,
        "label": "before",
        "git": {"sha": "abc", "branch": "main", "dirty": False},
        "created_at": time.time(),
        "suite": {"name": "orders", "repeat": 2, "warmup": 1, "requests": [{"path": "/a"}]},
        **kw,
    }
    store.create_check_run(run)
    store._check_cache = None
    return run


# schema ----------------------------------------------------------------------------
def test_schema_v4_fresh_and_v3_upgrade(tmp_path):
    path = str(tmp_path / "r.db")
    s = SQLiteStore(path, background=False)
    assert SCHEMA_VERSION == 4
    assert {"check_suites", "check_runs", "check_results"} <= tables(s)
    t = make_trace()
    s.save(t)
    # make it look like a v3 file
    for table in ("check_suites", "check_runs", "check_results"):
        s._conn.execute(f"DROP TABLE {table}")
    s._conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    s.close()
    s = SQLiteStore(path, background=False)
    try:
        assert {"check_suites", "check_runs", "check_results"} <= tables(s)
        assert rows(s, "SELECT value FROM meta WHERE key = 'schema_version'") == [("4",)]
        assert s.get(t.id) is not None  # traces untouched
        s.create_check_suite("x", {"requests": [{"path": "/"}]})
    finally:
        s.close()
    s = SQLiteStore(path, background=False)  # idempotent: reopening migrates nothing
    try:
        assert len(s.list_check_suites()) == 1
    finally:
        s.close()


def test_migration_is_one_transaction(tmp_path, monkeypatch):
    path = str(tmp_path / "r.db")
    SQLiteStore(path, background=False).close()
    conn = sqlite3.connect(path)
    for table in ("check_suites", "check_runs", "check_results"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    from redline_core.storage import sqlite as sqlite_mod

    monkeypatch.setattr(sqlite_mod, "SCHEMA_V4", sqlite_mod.SCHEMA_V4 + ";CREATE TABLE broken ((")
    with pytest.raises(sqlite3.Error):
        SQLiteStore(path, background=False)
    conn = sqlite3.connect(path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    conn.close()
    assert "check_suites" not in names and version == ("3",)  # rolled back as a whole
    monkeypatch.undo()
    s = SQLiteStore(path, background=False)
    assert "check_suites" in tables(s)
    s.close()


# suites ----------------------------------------------------------------------------
def test_suite_crud(store):
    suite = {"name": "orders", "requests": [{"path": "/a"}, {"path": "/b"}]}
    rec = store.create_check_suite("orders", suite)
    assert rec["id"] and rec["suite"] == suite
    assert store.get_check_suite(rec["id"])["suite"] == suite
    [item] = store.list_check_suites()
    assert item["requests"] == 2 and item["last_baseline"] is None and item["last_compare"] is None
    time.sleep(0.01)
    upd = store.update_check_suite(rec["id"], "renamed", {"requests": [{"path": "/c"}]})
    assert upd["name"] == "renamed" and upd["updated_at"] > rec["updated_at"]
    assert store.update_check_suite("nope", "x", {}) is None
    new_run(store, "b1", suite_id=rec["id"])
    new_run(store, "c2", suite_id=rec["id"], kind="compare", baseline_id="b1")
    [item] = store.list_check_suites()
    assert item["last_baseline"]["id"] == "b1" and item["last_compare"]["id"] == "c2"
    assert store.delete_check_suite(rec["id"]) == 1
    assert store.get_check_suite(rec["id"]) is None
    assert store.get_check_run("b1") is None and store.delete_check_suite(rec["id"]) == 0


# runs ------------------------------------------------------------------------------
def test_run_lifecycle_and_baseline_doc(store):
    new_run(store, "b1")
    run = store.get_check_run("b1")
    assert run["status"] == "running" and run["kind"] == "baseline"
    assert run["git"]["sha"] == "abc" and run["suite"]["name"] == "orders"
    assert run["name"] == "orders" and run["request_count"] == 1
    assert store.check_baseline("b1") is None  # not finished
    store.add_check_result("b1", 0, "GET /a", {"name": "GET /a", "perf": {"median": 3}})
    store.add_check_result("b1", 1, "GET /b", {"name": "GET /b"})
    store.add_check_result("b1", 0, "GET /a", {"name": "GET /a", "perf": {"median": 4}})
    store.set_check_progress("b1", {"done": 3, "total": 6})
    assert store.get_check_run("b1")["progress"] == {"done": 3, "total": 6}
    store.finish_check_run("b1", "done", {"requests": 2})
    run = store.get_check_run("b1")
    assert run["status"] == "done" and run["finished_at"] and run["summary"] == {"requests": 2}
    doc = store.check_baseline("b1")
    assert doc["format"] == "redline-check-baseline" and doc["version"] == 1
    assert [r["name"] for r in doc["requests"]] == ["GET /a", "GET /b"]
    assert doc["requests"][0]["perf"]["median"] == 4
    with pytest.raises(ValueError):
        store.finish_check_run("b1", "running")
    # a compare run snapshots its baseline's identity
    new_run(store, "c1", kind="compare", baseline_id="b1", label=None)
    c = store.get_check_run("c1")
    assert c["baseline"]["id"] == "b1" and c["baseline"]["label"] == "before"
    # the engine's start after the UI's pre-insert keeps created_at and the launcher's info
    created = c["created_at"]
    store.create_check_run({"id": "c1", "kind": "compare", "git": None, "suite": None})
    c = store.get_check_run("c1")
    assert c["created_at"] == created and c["git"]["sha"] == "abc" and c["suite"]["name"]
    assert [r["id"] for r in store.list_check_runs()] == ["c1", "b1"]
    assert [r["id"] for r in store.list_check_runs(kind="baseline")] == ["b1"]
    assert store.list_check_runs(suite_id="other") == []
    assert "suite" not in store.list_check_runs()[0]
    assert store.delete_check_run("b1") == 1 and store.get_check_results("b1") == []
    assert store.delete_check_run("b1") == 0
    with pytest.raises(ValueError):
        store.create_check_run({"id": "x", "kind": "load"})


def test_import_baseline(store):
    doc = {
        "format": "redline-check-baseline",
        "version": 1,
        "suite": {"name": "ci"},
        "label": "main",
        "git": {"sha": "f00"},
        "release": "1.2",
        "created_at": 1000.0,
        "requests": [{"name": "GET /a"}, {"name": "GET /b"}],
    }
    rid = store.import_check_baseline(doc, None)
    run = store.get_check_run(rid)
    assert run["status"] == "done" and run["source"] == "import" and run["release"] == "1.2"
    assert run["created_at"] == 1000.0
    assert [r["name"] for r in store.check_baseline(rid)["requests"]] == ["GET /a", "GET /b"]


def test_retention_keeps_recent_running_and_referenced(store):
    t0 = time.time() - 1000
    new_run(store, "old-base", created_at=t0, status="done")
    store.finish_check_run("old-base", "done")
    new_run(store, "old-running", created_at=t0 + 1)
    for i in range(5):
        new_run(store, f"r{i}", created_at=t0 + 10 + i, kind="compare", baseline_id="old-base")
        store.finish_check_run(f"r{i}", "done")
    new_run(store, "other-suite", suite_id="s2", created_at=t0)
    assert store.prune_check_runs("s1", keep=3) == 2  # r0, r1
    left = {r["id"] for r in store.list_check_runs(limit=100)}
    assert left == {"old-base", "old-running", "r2", "r3", "r4", "other-suite"}
    # once nothing kept refers to it, the old baseline goes too
    for i in range(3):
        new_run(store, f"n{i}", created_at=t0 + 100 + i, kind="baseline")
        store.finish_check_run(f"n{i}", "done")
    store.prune_check_runs("s1", keep=3)
    left = {r["id"] for r in store.list_check_runs(limit=100)}
    assert left == {"old-running", "n0", "n1", "n2", "other-suite"}


def test_retention_runs_on_create(store, monkeypatch):
    monkeypatch.setattr("redline_core.storage.checks.CHECK_RETENTION_PER_SUITE", 2)
    for i in range(4):
        new_run(store, f"x{i}", created_at=time.time() + i)
        store.finish_check_run(f"x{i}", "done")
    assert len(store.list_check_runs(limit=100)) == 2


def test_dead_check_runs_are_reaped(tmp_path):
    s = SQLiteStore(str(tmp_path / "r.db"), background=False)
    try:
        new_run(s, "dead1", created_at=time.time() - 60)
        new_run(s, "young", created_at=time.time())
        assert s.get_check_run("dead1")["status"] == "running"  # no runs dir: left alone
        os.makedirs(tmp_path / "runs")
        run = s.get_check_run("dead1")
        assert run["status"] == "failed" and run["error"] == DEAD_CHECK_MESSAGE
        assert s.get_check_run("young")["status"] == "running"
        (tmp_path / "runs" / "alive.pid").write_text(str(os.getpid()))
        new_run(s, "alive", created_at=time.time() - 60)
        assert s.get_check_run("alive")["status"] == "running"
    finally:
        s.close()


# tagging and the keep rule -----------------------------------------------------------
def _call(rec, header, path="/orders"):
    with rec.recording(method="GET", path=path, headers=header) as b:
        pass
    return rec.last_trace, b


def test_header_parsing():
    rec = Recorder(Settings(), None)
    t, _ = _call(rec, {"X-Redline-Check": "abc123;req=2;n=7"})
    assert t.tags["check_run_id"] == "abc123" and t.tags["check_req"] == "2"
    assert t.tags["check_n"] == "7"
    t, _ = _call(rec, [(b"x-redline-check", b" r_1-x ; n=-1 ; req=0 ")])
    assert (t.tags["check_run_id"], t.tags["check_req"], t.tags["check_n"]) == ("r_1-x", "0", "-1")
    for bad in ("", ";req=1", "a b;req=1", "x;req=-1", "x;req=abc", "x" * 65 + ";req=1", "x"):
        t, _ = _call(rec, {"X-Redline-Check": bad})
        assert "check_run_id" not in t.tags, bad
    t, _ = _call(rec, {"X-Redline-Check": "x;req=1;n=zz"})
    assert "check_n" not in t.tags and t.tags["check_req"] == "1"


def test_keep_rule_sampled_out(store):
    rec = Recorder(Settings(sample_rate=0.0, always_keep_slower_than_ms=1e9), store)
    new_run(store, "live")
    plain, _ = _call(rec, {})
    assert store.get(plain.id) is None  # sampled out
    good, _ = _call(rec, {"X-Redline-Check": "live;req=0;n=1"})
    stored = store.get(good.id)
    assert stored is not None and stored.tags["sampled_by"] == "check"
    assert stored.tags["check_run_id"] == "live"
    forged, _ = _call(rec, {"X-Redline-Check": "made-up;req=0;n=1"})
    assert forged.tags["sampled_by"] == "check"  # provisionally...
    assert store.get(forged.id) is None  # ...dropped by the writer
    # a check that ended more than a minute ago no longer keeps anything
    store.finish_check_run("live", "done")
    store._conn.execute("UPDATE check_runs SET finished_at = ?", (time.time() - 61,))
    store._check_cache = None
    late, _ = _call(rec, {"X-Redline-Check": "live;req=0;n=1"})
    assert store.get(late.id) is None


def test_keep_rule_sampled_in_strips_invalid_tags(store):
    rec = Recorder(Settings(), store)
    t, _ = _call(rec, {"X-Redline-Check": "nope;req=0;n=1"})
    stored = store.get(t.id)
    assert stored is not None and stored.tags["sampled_by"] == "all"
    assert not any(k.startswith("check_") for k in stored.tags)


def test_keep_rule_needs_a_validating_store():
    saved = []

    class Plain:
        def save(self, trace):
            saved.append(trace)

    rec = Recorder(Settings(sample_rate=0.0, always_keep_slower_than_ms=1e9), Plain())
    _call(rec, {"X-Redline-Check": "live;req=0;n=1"})
    assert saved == []  # a store that cannot validate the tag never gets forced data


def test_known_check_runs_cache(store):
    assert store.known_check_runs() == set()
    new_run(store, "a")  # create_check_run invalidates the cache
    assert store.known_check_runs() == {"a"}
    store._conn.execute("DELETE FROM check_runs")
    assert store.known_check_runs() == {"a"}  # cached for a second
    store._check_cache = (time.monotonic() - 2, {"a"})
    assert store.known_check_runs() == set()


# sinks -----------------------------------------------------------------------------
def test_redact_suite():
    from redline_core.check.sink import is_placeholder, redact_suite

    suite = {
        "base_url": "http://u:pw@h",
        "headers": {"Authorization": "Bearer abc", "Accept": "json", "Cookie": "${env:C}"},
        "requests": [{"path": "/a?token=x", "headers": {"X-Api-Key": "k", "X-Token": "${env:T}"}}],
    }
    out = redact_suite(suite)
    assert out["headers"] == {"Authorization": MASK, "Accept": "json", "Cookie": "${env:C}"}
    assert out["requests"][0]["headers"] == {"X-Api-Key": MASK, "X-Token": "${env:T}"}
    assert "pw" not in out["base_url"] and "token=x" not in out["requests"][0]["path"]
    assert is_placeholder("Bearer ${env:API_TOKEN}") and not is_placeholder("${env:A}x")
    assert suite["headers"]["Authorization"] == "Bearer abc"  # input untouched


def test_store_check_sink(store):
    from redline_core.check.sink import CheckSink, StoreCheckSink

    sink = StoreCheckSink(store)
    assert isinstance(sink, CheckSink)

    class Obj:
        def to_dict(self):
            return {"name": "x", "headers": {"Authorization": "Bearer s"}, "requests": []}

    sink.start(
        {"id": "s1", "suite_id": None, "kind": "baseline", "baseline_id": None, "label": "L",
         "git": None, "created_at": time.time(), "suite": Obj()}
    )  # fmt: skip
    assert store.get_check_run("s1")["suite"]["headers"]["Authorization"] == MASK
    sink.result("s1", 0, "GET /a", {"perf": {"samples_ms": (1.0, 2.0)}, "obj": Obj()})
    assert store.get_check_results("s1")[0]["data"]["perf"]["samples_ms"] == [1.0, 2.0]
    sink.finish("s1", "weird", None, "boom password=hunter2")
    run = store.get_check_run("s1")
    assert run["status"] == "failed" and "hunter2" not in run["error"]
    sink.result("missing", "x", "n", {})  # errors are logged, never raised


def _check_trace(run_id, req, n, *, ms, queries=2, issues=False):
    cs = make_callsite()
    spans = [sql_span(N1_SQL, [i], callsite=cs) for i in range(queries)]
    spans.append(sql_span("SELECT 1"))
    t = make_trace(
        spans=spans,
        duration_ms=ms,
        tags={"check_run_id": run_id, "check_req": str(req), "check_n": str(n)},
    )
    if issues:
        t.issues = detect(t)
    return t


def test_server_metrics(store):
    from redline_core.check.sink import ServerMetrics, StoreServerMetrics

    new_run(store, "m1", suite={"repeat": 2, "warmup": 1, "requests": [{}, {}]})
    for n, ms in enumerate((10, 30, 20), start=1):
        store.save(_check_trace("m1", 0, n, ms=ms, queries=6, issues=True))
    for n, ms in enumerate((5, 7), start=1):
        store.save(_check_trace("m1", 1, n, ms=ms, queries=1))
    store.save(_check_trace("m1", 1, 0, ms=500))  # a warm-up call (n=0) is ignored
    store.save(_check_trace("other", 1, 1, ms=1))
    metrics = StoreServerMetrics(store)
    assert isinstance(metrics, ServerMetrics)
    start = time.monotonic()
    out = metrics.for_check("m1", timeout_s=5)
    assert time.monotonic() - start < 2  # 6 calls expected and stored: no waiting
    assert set(out) == {0, 1}
    r0 = out[0]
    assert r0["calls"] == 3 and r0["duration_ms_median"] == 20
    assert r0["queries_median"] == 7 and r0["queries_max"] == 7
    assert r0["sample_trace_id"] and store.get(r0["sample_trace_id"]).duration_ms == 20
    assert {v["count"] for v in r0["sql"].values()} == {6, 1}
    n1 = next(v for v in r0["sql"].values() if v["count"] == 6)
    assert '"items"' in n1["sql"] and n1["calls"] == 3 and n1["max"] == 6
    assert {i["kind"] for i in r0["issues"]} >= {"n_plus_one"}
    assert all(i["calls"] == 3 and len(i["fingerprint"]) == 16 for i in r0["issues"])
    assert out[1]["duration_ms_median"] == 6 and out[1]["issues"] == []


def test_server_metrics_nothing_arrives(store):
    from redline_core.check.sink import StoreServerMetrics

    start = time.monotonic()
    assert StoreServerMetrics(store).for_check("none", timeout_s=0.3) == {}
    assert 0.25 <= time.monotonic() - start < 2


def test_server_metrics_settles_without_expected_count(store):
    from redline_core.check.sink import StoreServerMetrics

    new_run(store, "free", suite=None)
    store.save(_check_trace("free", 0, 1, ms=3))
    m = StoreServerMetrics(store)
    m.SETTLE_S = 0.2
    start = time.monotonic()
    assert m.for_check("free", timeout_s=5)[0]["calls"] == 1
    assert time.monotonic() - start < 2


def test_server_metrics_background_writer(tmp_path):
    from redline_core.check.sink import StoreServerMetrics

    s = SQLiteStore(str(tmp_path / "r.db"))  # background writer: for_check flushes it
    try:
        new_run(s, "bg")
        s.save(_check_trace("bg", 0, 1, ms=3))
        m = StoreServerMetrics(s)
        m.SETTLE_S = 0.2
        assert m.for_check("bg", timeout_s=3)[0]["calls"] == 1
    finally:
        s.close()


def test_load_baseline(store):
    from redline_core.check.sink import load_baseline

    new_run(store, "b1")
    store.add_check_result("b1", 0, "GET /a", {"name": "GET /a"})
    assert load_baseline(store, "b1") is None  # still running
    store.finish_check_run("b1", "done", {})
    doc = load_baseline(store, "b1")
    assert doc["format"] == "redline-check-baseline" and doc["id"] == "b1"
    assert doc["status"] == "done" and isinstance(doc["suite"], dict)
    assert doc["requests"] == [{"name": "GET /a"}]
    assert load_baseline(store, "nope") is None and load_baseline(object(), "b1") is None
