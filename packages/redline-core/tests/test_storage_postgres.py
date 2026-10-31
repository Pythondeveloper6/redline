"""The shared PostgreSQL store against a real server.

Skipped unless ``REDLINE_TEST_POSTGRES_DSN`` names a reachable PostgreSQL (the CI
``postgres`` job sets it; locally: ``docker run -d -e POSTGRES_PASSWORD=pg -p 5432:5432
postgres:17`` and ``REDLINE_TEST_POSTGRES_DSN=postgresql://postgres:pg@localhost/postgres``).
Every test gets its own schema, dropped afterwards.

Besides the PostgreSQL-specific tests below, the store-level tests of the SQLite suites
(``test_storage``, ``test_storage_v2``, ``test_check_store``) run here again against
PostgreSQL: they are imported, so they use this module's ``store`` fixture.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import time
import uuid

import pytest

from redline_core.conf import Settings
from redline_core.detect import detect
from redline_core.runs import Run, RunStep
from redline_core.storage import Filters, PostgresStore, SQLiteStore, Store, store_from_settings

from . import test_check_store as _check_store
from . import test_storage as _storage
from . import test_storage_v2 as _storage_v2
from .conftest import make_trace, sql_span

DSN = os.environ.get("REDLINE_TEST_POSTGRES_DSN", "")


def _server_ok() -> str | None:
    if not DSN:
        return "set REDLINE_TEST_POSTGRES_DSN to run the PostgreSQL store tests"
    try:
        import psycopg
    except ImportError:
        return "psycopg is not installed"
    try:
        psycopg.connect(DSN, connect_timeout=3).close()
    except Exception as exc:
        return f"PostgreSQL at REDLINE_TEST_POSTGRES_DSN is unreachable ({type(exc).__name__})"
    return None


_SKIP = _server_ok()
pytestmark = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")


def _drop(schema: str) -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def schema():
    name = "rl_" + uuid.uuid4().hex[:12]
    yield name
    _drop(name)


@pytest.fixture
def store(schema):
    s = PostgresStore(DSN, schema=schema, background=False)
    yield s
    s.close()


# ── the SQLite store suites, re-run on PostgreSQL ───────────────────────────────────────
#: Tests of those modules that are about the SQLite file itself (WAL, paths, migrations of
#: old files, pid files beside it) rather than the store's behaviour.
_SQLITE_ONLY = {
    "test_store_protocol_and_separate_wal_file",
}


def _reuse(module) -> None:
    for name, fn in vars(module).items():
        if not name.startswith("test_") or not callable(fn) or name in _SQLITE_ONLY:
            continue
        params = inspect.signature(fn).parameters
        if "store" not in params or "tmp_path" in params:
            continue
        globals()[f"{name}__{module.__name__.rsplit('.', 1)[-1]}"] = fn


for _module in (_storage, _storage_v2, _check_store):
    _reuse(_module)


# ── PostgreSQL-specific behaviour ───────────────────────────────────────────────────────
def test_protocol_url_masking_and_repr(store):
    assert isinstance(store, Store)
    assert "***" in store.path and "***" in repr(store)
    if ":" in DSN.split("://", 1)[1].split("@", 1)[0]:
        password = DSN.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
        assert password not in store.path


def test_round_trip_trace_with_profile_tags_and_nul(store):
    t = make_trace(
        path="/shop/orders/7",
        tags={"request_id": "abc", "note": "nul\x00byte"},
        profile={"shared": {"frames": []}, "profiles": []},
        spans=[sql_span("SELECT * FROM orders WHERE id = %s", [7])],
    )
    t.issues = detect(t, Settings())
    store.save(t)
    got = store.get(t.id)
    assert got is not None and got.id == t.id and got.path == t.path
    assert got.profile == t.profile and store.has_profile(t.id)
    assert got.spans[0].data == t.spans[0].data
    # NUL cannot be stored in PostgreSQL text; it is replaced rather than failing the batch
    assert store.list(Filters(tag=("note", "nul�byte"))).total == 1


def test_list_filters_and_pagination(store):
    now = time.time()
    for i in range(23):
        store.save(
            make_trace(
                path=f"/shop/items/{i}",
                route="/shop/items/{id}",
                method="GET" if i % 2 else "POST",
                status=500 if i % 5 == 0 else 200,
                duration_ms=float(i),
                started_at=now - 100 + i,
            )
        )
    store.save(make_trace(path="/100%_off", started_at=now - 1))
    seen, cursor = [], None
    while True:
        page = store.list(limit=5, cursor=cursor)
        assert page.total == 24
        seen += [item["id"] for item in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 24
    assert store.list(Filters(method="POST")).total == 12
    assert store.list(Filters(status="5xx")).total == 5
    assert store.list(Filters(min_ms=20)).total == 3
    assert store.list(Filters(q="100%_")).total == 1  # LIKE wildcards are literal
    assert store.list(Filters(q="ITEMS/1")).total == 11  # case-insensitive


def _fixture_traces(now: float) -> list:
    traces = []
    for i in range(30):
        spans = [
            sql_span('SELECT * FROM "order_lines" WHERE "order_id" = %s', [j], duration_ms=1.5)
            for j in range(6 if i % 3 == 0 else 1)
        ]
        t = make_trace(
            path=f"/orders/{i}",
            route="/orders/{id}" if i % 4 else "/customers/{id}",
            method="GET",
            status=503 if i % 7 == 0 else 200,
            duration_ms=5.0 + i * 3,
            started_at=now - 3000 + i * 90,
            spans=spans,
            tags={"release": "v1" if i < 15 else "v2"},
        )
        t.issues = detect(t, Settings())
        traces.append(t)
    return traces


def test_aggregates_match_sqlite_on_the_same_data(store, tmp_path):
    now = time.time()
    lite = SQLiteStore(str(tmp_path / "parity.db"), background=False)
    try:
        for t in _fixture_traces(now):
            lite.save(t)
            store.save(t)
        assert store.count() == lite.count() == 30
        assert [e.to_dict() for e in store.endpoints(3600, now=now)] == [
            e.to_dict() for e in lite.endpoints(3600, now=now)
        ]
        assert [i.to_dict() for i in store.issues(3600, now=now)] == [
            i.to_dict() for i in lite.issues(3600, now=now)
        ]
        assert [s.to_dict() for s in store.sql_workload(3600, now=now)] == [
            s.to_dict() for s in lite.sql_workload(3600, now=now)
        ]
        assert (
            store.history(None, None, now - 7200, now + 3600).to_dict()
            == lite.history(None, None, now - 7200, now + 3600).to_dict()
        )
        assert [r.to_dict() for r in store.issue_index()] == [
            r.to_dict() for r in lite.issue_index()
        ]
        assert [r.release for r in store.releases()] == [r.release for r in lite.releases()]
        assert (
            store.list(Filters(has_issues=True)).total == lite.list(Filters(has_issues=True)).total
        )
    finally:
        lite.close()


def test_prune_by_rows_and_age(store):
    now = time.time()
    for i in range(10):
        store.save(make_trace(started_at=now - 1000 + i))
    store.save(make_trace(started_at=now - 30 * 86_400))
    assert store.prune(max_age_s=86_400, max_rows=0) == 1
    assert store.prune(max_age_s=0, max_rows=4) == 6
    assert store.count() == 4
    assert store.prune(max_age_s=0, max_rows=0) == 0


def test_runs_and_checks(store):
    run = Run(id="load1", name="ramp")
    run.steps = [RunStep(step=0, target_rps=5.0)]
    store.save_run(run)
    store.save_run_step("load1", RunStep(step=1, target_rps=10.0, sent=3, ok=3))
    store.finish_run("load1", "done", {"requests": 3})
    got = store.get_run("load1")
    assert got is not None and got.status == "done" and [s.step for s in got.steps] == [0, 1]
    assert [r.id for r in store.list_runs()] == ["load1"]
    suite = store.create_check_suite("shop", {"requests": [{"name": "home"}]})
    rid = store.create_check_run({"suite_id": suite["id"], "kind": "baseline", "suite": {}})
    store.add_check_result(rid, 0, "home", {"ok": True})
    store.add_check_result(rid, 0, "home", {"ok": False})  # replaced
    store.finish_check_run(rid, "done", {"passed": 1})
    assert store.get_check_results(rid) == [{"idx": 0, "name": "home", "data": {"ok": False}}]
    assert store.check_baseline(rid)["requests"] == [{"ok": False}]
    assert store.delete_run("load1") == 1 and store.delete_check_suite(suite["id"]) == 1


def test_schema_migration_is_idempotent(schema):
    first = PostgresStore(DSN, schema=schema, background=False)
    first.save(make_trace())
    first.close()
    for _ in range(3):
        again = PostgresStore(DSN, schema=schema, background=False)
        assert again.schema_version() == 4 and again.count() == 1
        again.close()


WRITER = """
import sys, time
from redline_core.model import Trace
from redline_core.storage import PostgresStore

dsn, schema, worker, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
store = PostgresStore(dsn, schema=schema, batch_size=7, prune_every=10_000)
try:
    for i in range(count):
        store.save(Trace(f"{worker}-{i}-{time.time_ns()}", "other", "TASK", "job"))
    if not store.flush(30) or store.dropped:
        raise SystemExit(4)
    print(store.schema_version())
finally:
    store.close()
"""


def _spawn(schema: str, worker: str, count: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", WRITER, DSN, schema, worker, str(count)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_concurrent_processes_create_the_schema_once_and_all_writes_land(schema):
    # Several independent processes start on an empty schema at once: the migration runs
    # under the advisory lock (no "already exists" races) and every trace is stored.
    processes = [_spawn(schema, f"w{i}", 80) for i in range(5)]
    results = [p.communicate(timeout=120) for p in processes]
    for process, (out, err) in zip(processes, results, strict=True):
        assert process.returncode == 0, err
        assert out.strip() == "4"
    store = PostgresStore(DSN, schema=schema, background=False)
    try:
        assert store.count() == 400
        ids = {item["id"] for item in _all(store)}
        assert {f"w{i}" for i in range(5)} == {i.split("-")[0] for i in ids}
        seqs = [r["seq"] for r in _tail(store)]
        assert len(seqs) == len(set(seqs)) == 400  # one insertion sequence across processes
    finally:
        store.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork()")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_forked_workers_share_a_store_created_before_fork(schema):
    # gunicorn --preload: the store is built in the parent, the workers are forked.
    parent = PostgresStore(DSN, schema=schema, batch_size=5)
    parent.save(make_trace(path="/parent"))
    assert parent.flush(10)  # the parent holds a live connection when it forks
    pids = []
    for w in range(4):
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            code = 1
            try:
                for i in range(50):
                    parent.save(make_trace(path=f"/w{w}/{i}"))
                code = 0 if parent.flush(30) and not parent.dropped else 3
                parent.close()
            finally:
                os._exit(code)
        pids.append(pid)
    for pid in pids:
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    # the children never used (or closed) the parent's connection
    parent.save(make_trace(path="/parent-after"))
    assert parent.flush(10) and parent.dropped == 0
    assert parent.count() == 202
    parent.close()


def _tail(store) -> list[dict]:
    out, seq = [], 0
    while True:
        rows = store.traces_since(0, after_seq=seq, limit=500)
        if not rows:
            return out
        out += rows
        seq = rows[-1]["seq"]


def _all(store) -> list[dict]:
    out, cursor = [], None
    while True:
        page = store.list(limit=500, cursor=cursor)
        out += page.items
        cursor = page.next_cursor
        if cursor is None:
            return out


def test_background_writer_and_reconnect(store, schema):
    s = PostgresStore(DSN, schema=schema)
    try:
        s.save(make_trace(path="/a"))
        assert s.flush(10)
        s._conn._raw.close()  # the server or a proxy dropped the connection
        s.save(make_trace(path="/b"))
        assert s.flush(10)
        assert s.count() == 2 and s.dropped == 0
    finally:
        s.close()


def test_store_from_settings_selects_postgres(schema):
    settings = Settings(storage_url=DSN, max_rows=5)
    s = store_from_settings(settings, schema=schema, background=False)
    try:
        assert isinstance(s, PostgresStore) and s.max_rows == 5
    finally:
        s.close()
    s = SQLiteStore.from_settings(settings, schema=schema, background=False)
    try:
        assert isinstance(s, PostgresStore)
    finally:
        s.close()
