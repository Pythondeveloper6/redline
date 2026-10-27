"""Review fixes: concurrent writers, clear() rollback, live tail order, profile caps,
dead runs, pid probe, unmatched routes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from redline_core import stream
from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.load import control
from redline_core.load.profile import MAX_STEPS, Profile, ScenarioError, parse_profile
from redline_core.runs import Run
from redline_core.storage import SQLiteStore
from redline_core.storage.derive import UNMATCHED

from .conftest import make_trace
from .test_storage_v2 import n1_trace, rows


# 1. concurrent writers ------------------------------------------------------------
def test_two_stores_on_one_file_lose_no_traces(tmp_path):
    path = str(tmp_path / "shared.db")
    stores = [SQLiteStore(path, background=False) for _ in range(2)]
    per_thread, threads_per_store = 40, 2
    errors: list[BaseException] = []

    def work(store):
        try:
            for _ in range(per_thread):
                store.save(make_trace())
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=work, args=(s,)) for s in stores for _ in range(threads_per_store)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert stores[0].count() == per_thread * threads_per_store * len(stores)
    seqs = [r[0] for r in rows(stores[0], "SELECT saved_seq FROM traces")]
    assert len(set(seqs)) == len(seqs)  # unique insertion numbers across writers
    for s in stores:
        s.close()


# 2. clear() rollback --------------------------------------------------------------
class _FailingConn:
    def __init__(self, conn):
        self._conn = conn
        self.fail = True

    def execute(self, sql, *args):
        if self.fail and sql.startswith("DELETE FROM traces"):
            raise RuntimeError("boom")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_clear_rolls_back_on_error(store):
    store.save(make_trace())
    wrapper = _FailingConn(store._conn)
    store._conn = wrapper
    with pytest.raises(RuntimeError):
        store.clear()
    wrapper.fail = False
    assert not store._conn.in_transaction
    store.save(make_trace())  # later writes still work
    assert store.count() == 2


# 3. live tail follows insertion order ---------------------------------------------
def test_live_tail_delivers_a_slow_request_that_started_earlier(store):
    now = time.time()
    api = RedlineAPI(store, Settings())
    slow = make_trace(started_at=now - 10)  # started first, finishes last
    fast1, fast2 = make_trace(started_at=now - 5), make_trace(started_at=now - 4)
    store.save(fast1)
    store.save(fast2)
    cursor = stream._Cursor(api, now - 20)
    first = cursor.poll()
    assert len(first) == 2
    store.save(slow)
    second = cursor.poll()
    assert len(second) == 1 and slow.id.encode() in second[0]
    assert cursor.poll() == []


def test_sse_id_is_the_seq_and_resumes(store):
    api = RedlineAPI(store, Settings())
    t1, t2 = make_trace(), make_trace()
    store.save(t1)
    store.save(t2)
    items = api.poll(0)
    assert [i["id"] for i in items] == [t1.id, t2.id]
    assert f"id: {items[0]['seq']}\n".encode() in stream.format_event(items[0])
    assert [i["id"] for i in api.poll(0, after_seq=items[0]["seq"])] == [t2.id]
    assert stream.parse_after({}, {"Last-Event-ID": "7"}) == 7
    assert stream.parse_after({"after": "3"}) == 3
    assert stream.parse_after({}, {"Last-Event-ID": "abc"}) is None


def test_seq_is_never_reused_and_migration_backfills(tmp_path):
    path = str(tmp_path / "m.db")
    s = SQLiteStore(path, background=False)
    a, b = make_trace(), make_trace()
    s.save(a)
    s.save(b)
    s.delete(b.id)
    c = make_trace()
    s.save(c)  # does not reuse b's number
    seqs = {r[0]: r[1] for r in rows(s, "SELECT id, saved_seq FROM traces")}
    assert seqs[c.id] > seqs[a.id] and seqs[c.id] != 2
    s._conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    s._conn.execute("UPDATE traces SET saved_seq = NULL")
    s._conn.execute("DELETE FROM meta WHERE key = 'last_seq'")
    s.close()
    s = SQLiteStore(path, background=False)  # a v2 file is migrated to v3 in place
    assert all(r[0] is not None for r in rows(s, "SELECT saved_seq FROM traces"))
    d = make_trace()
    s.save(d)
    top = max(r[0] for r in rows(s, "SELECT saved_seq FROM traces WHERE id != ?", (d.id,)))
    assert rows(s, "SELECT saved_seq FROM traces WHERE id = ?", (d.id,))[0][0] > top
    s.close()


# 4. profile caps ---------------------------------------------------------------------
def test_unbounded_ramp_is_rejected_without_building_a_plan():
    started = time.perf_counter()
    with pytest.raises(ScenarioError):
        parse_profile(
            {"mode": "ramp", "start_rps": 1, "end_rps": 2, "duration_s": 1e6,
             "step_duration_s": 0.001}
        )  # fmt: skip
    assert time.perf_counter() - started < 1


@pytest.mark.parametrize(
    "data",
    [
        {"mode": "constant", "rps": 10, "duration_s": 10**7},
        {"mode": "constant", "rps": 10**7, "duration_s": 10},
        {"mode": "ramp", "start_rps": 1, "end_rps": 10**7, "duration_s": 60},
        {"mode": "steps", "start_rps": 1, "end_rps": 10**6, "step_rps": 1},
    ],
)
def test_absurd_profiles_rejected(data):
    with pytest.raises(ScenarioError):
        parse_profile(data)


def test_max_rps_and_duration_match_the_plan():
    for prof in (
        Profile.constant(5, 10),
        Profile.ramp(1, 50, 33, step_duration_s=5),
        Profile.ramp(100, 1, 33, step_duration_s=5),
        Profile.steps_of(10, 100, 30, 5),
        Profile.steps_of(10, 100, 30, 5, duration_s=12),
    ):
        plans = prof.plan()
        assert prof.step_count() == len(plans) < MAX_STEPS
        assert prof.max_rps == pytest.approx(max(p.target_rps for p in plans))
        assert prof.total_duration_s == pytest.approx(plans[-1].end_s)


def test_ui_runs_have_stricter_limits(tmp_path):
    cfg = {
        "requests": [{"path": "/a"}],
        "profile": {"mode": "constant", "rps": 50_000, "duration_s": 10},
    }
    with pytest.raises(ScenarioError):
        control.launch_background(cfg, "http://127.0.0.1:1", tmp_path / "r.db")
    assert not (tmp_path / "runs").exists()


# 5. runs stuck in "running" ------------------------------------------------------------
def test_dead_running_run_is_failed_and_deletable(tmp_path):
    store = SQLiteStore(tmp_path / "r.db", background=False)
    (tmp_path / "runs").mkdir()
    store.save_run(Run(id="dead1", name="x", created_at=time.time() - 60))
    store.save_run(Run(id="fresh", name="y", created_at=time.time()))
    store.save_run(Run(id="alive", name="z", created_at=time.time() - 60))
    (tmp_path / "runs" / "alive.pid").write_text(str(os.getpid()))
    api = RedlineAPI(store, Settings())
    _, body = api.dispatch("GET", "api/runs/dead1")
    assert body["status"] == "failed"
    assert body["summary"]["message"] == "the load process exited unexpectedly"
    assert store.get_run("dead1").finished_at is not None  # persisted
    assert store.get_run("fresh").status == "running"  # inside the grace period
    assert store.get_run("alive").status == "running"
    assert api.dispatch("DELETE", "api/runs/dead1")[0] == 200
    store.close()


def test_cli_closes_the_preinserted_run_when_the_run_cannot_start(tmp_path, monkeypatch):
    from redline_core import cli
    from redline_core.load import engine

    db = str(tmp_path / "r.db")
    store = SQLiteStore(db, background=False)
    store.save_run(Run(id="r1", name="ui"))
    store.close()
    scenario = tmp_path / "s.json"
    scenario.write_text(
        json.dumps({"name": "n", "base_url": "http://127.0.0.1:9", "requests": [{"path": "/a"}]})
    )
    monkeypatch.setattr(engine.importlib.util, "find_spec", lambda name, *a: None)
    code = cli.main(["load", str(scenario), "--store", db, "--run-id", "r1", "--quiet"])
    assert code != 0
    store = SQLiteStore(db, background=False)
    run = store.get_run("r1")
    assert run.status == "failed" and "httpx" in run.summary["message"]
    store.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent pid semantics")
def test_worker_notices_a_dead_parent():
    from redline_core.load.worker import _Worker

    w = _Worker.__new__(_Worker)
    w.cfg = {"parent_pid": os.getppid() + 100_000}
    assert w._parent_gone()
    w.cfg = {"parent_pid": os.getppid()}
    assert not w._parent_gone()


# 6. pid probe -------------------------------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="POSIX branch")
def test_pid_alive_posix():
    assert control._pid_alive(os.getpid())
    assert not control._pid_alive(0)
    assert not control._pid_alive(-5)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not control._pid_alive(proc.pid)


# 7. unmatched routes ---------------------------------------------------------------------
def test_unmatched_requests_share_one_history_key(store):
    for i in range(30):
        store.save(make_trace(path=f"/wp-admin/{i}x{os.urandom(3).hex()}", route=None, status=404))
    store.save(make_trace(path="/real/1", route="/real/{id}"))
    keys = {r[0] for r in rows(store, "SELECT route FROM rollups")}
    assert keys == {UNMATCHED, "/real/{id}"}
    assert rows(store, "SELECT COUNT(DISTINCT path) FROM traces")[0][0] > 20  # traces keep paths


def test_unmatched_issue_index_route(store):
    store.save(n1_trace(path="/scan/abc", route=None))
    assert {r[0] for r in rows(store, "SELECT route FROM issue_index")} == {UNMATCHED}
