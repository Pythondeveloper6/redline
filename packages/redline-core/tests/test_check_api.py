"""Refactor check API (``api/checks/…``) and the background runner (``check.control``)."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from redline_core.api import RawResponse, RedlineAPI
from redline_core.check import control
from redline_core.conf import Settings
from redline_core.recorder import Recorder
from redline_core.redact import MASK
from redline_core.storage import SQLiteStore

SUITE = {
    "name": "orders",
    "repeat": 2,
    "warmup": 1,
    "headers": {"Authorization": "Bearer s3cret", "X-Token": "${env:TOKEN}"},
    "thresholds": {"slower_pct": 10},
    "requests": [
        {"name": "list", "path": "/orders?page=1", "ignore": ["meta.id"]},
        {"name": "one", "method": "GET", "path": "/orders/1", "headers": {"Cookie": "sid=1"}},
    ],
}


class Launcher:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def __call__(self, suite, base_url, **kw):
        self.calls.append((suite, base_url, kw))
        rid = f"run{len(self.calls)}"
        self.store.create_check_run({"id": rid, "suite": suite, "git": None, **kw})
        return rid


@pytest.fixture
def env(store):
    launcher = Launcher(store)
    stopped = []

    def make(debug=True, **settings):
        api = RedlineAPI(
            store,
            Settings(**settings),
            check_launcher=launcher,
            check_stopper=lambda rid: stopped.append(rid) or True,
            debug=lambda: debug,
        )
        return api

    make.launcher = launcher
    make.stopped = stopped
    return make


def call(api, method, path, body=None, params=None, base_url="http://127.0.0.1:8000"):
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    return api.dispatch(method, path, params, body, base_url=base_url)


def make_suite(api, suite=SUITE):
    status, rec = call(api, "POST", "api/checks/suites", {"name": "orders", "suite": suite})
    assert status == 201, rec
    return rec


# suites --------------------------------------------------------------------------------
def test_suite_crud_masks_secrets(env, store):
    api = env()
    rec = make_suite(api)
    assert set(rec) >= {"id", "name", "suite"}
    assert rec["suite"]["headers"] == {"Authorization": MASK, "X-Token": "${env:TOKEN}"}
    assert rec["suite"]["requests"][1]["headers"]["Cookie"] == MASK
    # the store keeps the real value for the background run
    assert store.get_check_suite(rec["id"])["suite"]["headers"]["Authorization"] == "Bearer s3cret"
    status, got = call(api, "GET", f"api/checks/suites/{rec['id']}")
    assert status == 200 and got["suite"] == rec["suite"]
    # PUT the masked suite back: secrets are restored, edits applied
    edited = dict(got["suite"], repeat=5)
    status, upd = call(
        api, "PUT", f"api/checks/suites/{rec['id']}", {"name": "o2", "suite": edited}
    )
    assert status == 200 and upd["name"] == "o2" and upd["suite"]["repeat"] == 5
    raw = store.get_check_suite(rec["id"])["suite"]
    assert raw["headers"]["Authorization"] == "Bearer s3cret"
    assert raw["requests"][1]["headers"]["Cookie"] == "sid=1"
    status, listing = call(api, "GET", "api/checks/suites")
    [item] = listing["items"]
    assert item["requests"] == 2 and item["last_baseline"] is None and item["name"] == "o2"
    assert call(api, "DELETE", f"api/checks/suites/{rec['id']}") == (200, {"deleted": 1})
    assert call(api, "GET", f"api/checks/suites/{rec['id']}")[0] == 404
    assert call(api, "PUT", "api/checks/suites/nope", {"name": "x", "suite": SUITE})[0] == 404


@pytest.mark.parametrize(
    "suite,fragment",
    [
        ({"requests": []}, "requests"),
        ({"requests": [{"path": "/a", "method": "POST"}]}, "GET"),
        ({"requests": [{"path": "http://evil/a"}]}, "path"),
        ({"requests": [{"path": "/a"}], "bogus": 1}, "bogus"),
        ({"requests": [{"path": "/a"}, {"path": "/a"}]}, "duplicate"),
        ({"requests": [{"path": "/a"}], "repeat": 0}, "repeat"),
    ],
)
def test_suite_validation(env, suite, fragment, monkeypatch):
    import redline_core.api_checks as ac

    monkeypatch.setattr(ac, "_engine_suite", lambda: None)  # the built-in shim
    status, body = call(env(), "POST", "api/checks/suites", {"name": "x", "suite": suite})
    assert status == 400 and body["error"]["code"] == "invalid_suite"
    assert fragment.lower() in body["error"]["message"].lower()


def test_suite_validation_prefers_engine(env, monkeypatch):
    import redline_core.api_checks as ac

    seen = []

    class FakeSuite:
        @classmethod
        def from_dict(cls, d):
            seen.append(d)
            if d.get("name") == "bad":
                raise ValueError("engine says no")
            return cls()

    monkeypatch.setattr(ac, "_engine_suite", lambda: FakeSuite)
    api = env()
    assert call(api, "POST", "api/checks/suites", {"name": "x", "suite": SUITE})[0] == 201
    status, body = call(api, "POST", "api/checks/suites", {"suite": {**SUITE, "name": "bad"}})
    assert status == 400 and body["error"]["message"] == "engine says no" and seen


def test_body_errors(env):
    api = env()
    assert call(api, "POST", "api/checks/suites", b"{nope")[1]["error"]["code"] == "invalid_json"
    assert call(api, "POST", "api/checks/suites", {"name": "x"})[1]["error"]["code"] == (
        "invalid_body"
    )
    big = b'{"x": "' + b"a" * (1024 * 1024 + 1) + b'"}'
    status, body = call(api, "POST", "api/checks/suites", big)
    assert status == 413 and body["error"]["code"] == "payload_too_large"
    assert call(api, "PATCH", "api/checks/suites")[0] == 405
    assert call(api, "GET", "api/checks/whatever")[0] == 404


def test_export_toml(env):
    api = env()
    rec = make_suite(api)
    result = call(api, "GET", f"api/checks/suites/{rec['id']}/export")
    assert isinstance(result, RawResponse) and result.content_type.startswith("text/plain")
    assert 'filename="orders.toml"' in result.headers["Content-Disposition"]
    text = result.body.decode()
    assert "s3cret" not in text and "sid=1" not in text and "[[requests]]" in text
    if sys.version_info >= (3, 11):
        import tomllib

        data = tomllib.loads(text)
        assert data["name"] == "orders" and len(data["requests"]) == 2
        assert data["thresholds"]["slower_pct"] == 10


def test_feature_switch_and_read_only(env):
    off = env(features={"checks": False})
    status, body = call(off, "GET", "api/checks/suites")
    assert status == 404 and body["error"]["code"] == "feature_disabled"
    assert off.meta()[1]["features"]["checks"] is False
    ro = env(read_only=True)
    assert call(ro, "GET", "api/checks/suites")[0] == 200
    status, body = call(ro, "POST", "api/checks/suites", {"name": "x", "suite": SUITE})
    assert status == 403 and body["error"]["code"] == "read_only"
    assert ro.meta()[1]["features"]["checks_from_ui"] is False


# draft -----------------------------------------------------------------------------
def _seen(store, path, route, method="GET", status=200, headers=None, query=None):
    rec = Recorder(Settings(), store)
    b = rec.start_trace(method=method, path=path, route=route, headers=headers or {}, query=query)
    store.save(rec.finish_trace(b, status=status))


def test_draft_from_traffic(env, store):
    _seen(store, "/orders/7", "/orders/{id}", headers={"Authorization": "Bearer abc"})
    _seen(store, "/orders/8", "/orders/{id}")
    _seen(store, "/orders", "/orders", query="page=2&q=x")
    _seen(store, "/search", "/search", query="token=secret")  # redacted query: skipped
    _seen(store, "/orders", "/orders", method="POST")  # not read-only
    _seen(store, "/_redline/api/meta", "/_redline/api/meta")
    _seen(store, "/broken", "/broken", status=500)  # no successful sample
    _seen(store, "/me", "/me", headers={"Cookie": "sid=1"})
    status, suite = call(env(), "POST", "api/checks/suites/draft", {})
    assert status == 200
    by_name = {r["name"]: r for r in suite["requests"]}
    assert set(by_name) == {"GET /orders/{id}", "GET /orders", "GET /me"}
    assert by_name["GET /orders/{id}"]["path"] == "/orders/8"  # most recent
    assert by_name["GET /orders"]["path"] == "/orders?page=2&q=x"
    assert suite["headers"] == {
        "Authorization": "${env:REDLINE_CHECK_AUTHORIZATION}",
        "Cookie": "${env:REDLINE_CHECK_COOKIE}",
    }
    assert "abc" not in json.dumps(suite) and "sid=1" not in json.dumps(suite)
    # routes exactly as GET api/endpoints shows them (no method), and a limit
    status, only = call(env(), "POST", "api/checks/suites/draft", {"routes": ["/orders"]})
    assert [r["name"] for r in only["requests"]] == ["GET /orders"]
    status, one = call(env(), "POST", "api/checks/suites/draft", {"limit": 1})
    assert len(one["requests"]) == 1
    assert call(env(), "POST", "api/checks/suites/draft", {"limit": 0})[0] == 400
    assert call(env(), "POST", "api/checks/suites/draft", {"routes": [1]})[0] == 400
    # the draft validates as a suite
    assert call(env(), "POST", "api/checks/suites", {"name": "d", "suite": suite})[0] == 201


# runs ------------------------------------------------------------------------------
def test_start_run_rules(env, store):
    api = env(debug=False)
    rec = make_suite(api)
    body = {"suite_id": rec["id"], "kind": "baseline"}
    status, err = call(api, "POST", "api/checks/runs", body)
    assert status == 403 and err["error"]["code"] == "checks_from_ui_disabled"
    assert api.meta()[1]["features"]["checks_from_ui"] is False
    assert call(env(debug=False, checks_from_ui=True), "POST", "api/checks/runs", body)[0] == 202
    assert call(env(checks_from_ui=False), "POST", "api/checks/runs", body)[0] == 403
    api = env()  # debug mode: "auto" allows it
    assert api.meta()[1]["features"]["checks_from_ui"] is True
    status, err = call(api, "POST", "api/checks/runs", body)
    assert status == 409 and err["error"]["code"] == "run_active"  # the one above runs
    store.finish_check_run("run1", "done")
    status, started = call(api, "POST", "api/checks/runs", {**body, "label": "before"})
    assert status == 202 and started["id"] == "run2" and started["status"] == "running"
    suite, base_url, kw = env.launcher.calls[-1]
    assert suite["headers"]["Authorization"] == "Bearer s3cret"  # raw: only the child sees it
    assert base_url == "http://127.0.0.1:8000"
    assert kw == {"kind": "baseline", "suite_id": rec["id"], "baseline_id": None, "label": "before"}
    store.finish_check_run("run2", "done")
    for bad, code in (
        ({"suite_id": rec["id"], "kind": "load"}, 400),
        ({"kind": "baseline"}, 400),
        ({"suite_id": "nope", "kind": "baseline"}, 404),
        ({"suite_id": rec["id"], "kind": "compare", "baseline_id": "nope"}, 404),
    ):
        assert call(api, "POST", "api/checks/runs", bad)[0] == code, bad
    # the Host header never matters; a non-loopback derived URL is refused
    assert call(api, "POST", "api/checks/runs", body, base_url="http://evil.example")[0] == 400
    with_url = env(base_url="http://app.internal:9000")
    store.finish_check_run("run2", "done")
    assert call(with_url, "POST", "api/checks/runs", body, base_url=None)[0] == 202
    assert env.launcher.calls[-1][1] == "http://app.internal:9000"


def test_compare_uses_latest_baseline(env, store):
    api = env()
    rec = make_suite(api)
    compare = {"suite_id": rec["id"], "kind": "compare"}
    status, err = call(api, "POST", "api/checks/runs", compare)
    assert status == 400 and "baseline" in err["error"]["message"]
    call(api, "POST", "api/checks/runs", {"suite_id": rec["id"], "kind": "baseline"})
    # still running: not a finished baseline
    assert call(api, "POST", "api/checks/runs", {**compare, "baseline_id": "run1"})[0] == 400
    store.finish_check_run("run1", "failed")
    assert call(api, "POST", "api/checks/runs", {**compare, "baseline_id": "run1"})[0] == 400
    store._conn.execute("UPDATE check_runs SET status = 'done'")
    status, started = call(api, "POST", "api/checks/runs", compare)
    assert status == 202 and started["baseline_id"] == "run1"


def test_run_detail_list_stop_delete(env, store):
    api = env()
    rec = make_suite(api)
    call(api, "POST", "api/checks/runs", {"suite_id": rec["id"], "kind": "baseline"})
    store.set_check_progress("run1", {"done": 1, "total": 6, "current": "list"})
    store.add_check_result("run1", 0, "list", {"name": "list", "perf": {"median": 1}})
    status, run = call(api, "GET", "api/checks/runs/run1")
    assert status == 200 and run["status"] == "running"
    assert run["progress"] == {"done": 1, "total": 6, "current": "list"}
    assert run["requests"] == [{"name": "list", "perf": {"median": 1}}] and run["report"] is None
    for key in ("id", "suite_id", "kind", "baseline_id", "label", "git", "created_at",
                "finished_at", "summary", "error"):  # fmt: skip
        assert key in run
    assert run["suite"]["headers"]["Authorization"] == MASK  # stored redacted with the run
    assert call(api, "DELETE", "api/checks/runs/run1")[1]["error"]["code"] == "run_active"
    assert call(api, "DELETE", f"api/checks/suites/{rec['id']}")[0] == 409
    assert call(api, "POST", "api/checks/runs/run1/stop") == (200, {"stopped": True})
    assert env.stopped == ["run1"]
    store.finish_check_run("run1", "done", {"requests": 2})
    assert call(api, "POST", "api/checks/runs/run1/stop") == (200, {"stopped": False})
    call(
        api, "POST", "api/checks/runs", {"suite_id": rec["id"], "kind": "compare", "label": "after"}
    )
    store.add_check_result("run2", 0, "list", {"name": "list", "perf": {"verdict": "same"}})
    store.finish_check_run("run2", "done", {"counts": {"same": 1}})
    status, cmp_run = call(api, "GET", "api/checks/runs/run2")
    report = cmp_run["report"]
    assert report["format"] == "redline-check-report" and report["version"] == 1
    assert report["baseline"]["id"] == "run1" and report["run"]["id"] == "run2"
    assert report["summary"] == {"counts": {"same": 1}}
    assert report["requests"] == [{"name": "list", "perf": {"verdict": "same"}}]
    status, listing = call(api, "GET", "api/checks/runs", params={"suite_id": rec["id"]})
    assert [r["id"] for r in listing["items"]] == ["run2", "run1"]
    assert call(api, "GET", "api/checks/runs", params={"kind": "x"})[0] == 400
    status, listing = call(api, "GET", "api/checks/suites")
    item = listing["items"][0]
    assert item["last_baseline"]["id"] == "run1" and item["last_compare"]["status"] == "done"
    assert item["last_compare"]["summary"] == {"counts": {"same": 1}}
    assert call(api, "DELETE", "api/checks/runs/run2") == (200, {"deleted": 1})
    assert call(api, "GET", "api/checks/runs/run2")[1]["error"]["code"] == "check_run_not_found"
    assert call(api, "POST", "api/checks/runs/nope/stop")[0] == 404


def test_no_hooks_without_file_store():
    api = RedlineAPI(SQLiteStore(":memory:", background=False), Settings(), debug=lambda: True)
    status, rec = call(api, "POST", "api/checks/suites", {"name": "x", "suite": SUITE})
    body = {"suite_id": rec["id"], "kind": "baseline"}
    status, err = call(api, "POST", "api/checks/runs", body)
    assert status == 501 and err["error"]["code"] == "not_supported"


def test_custom_store_without_check_methods():
    class Bare:
        def count(self):
            return 0

    status, err = call(RedlineAPI(Bare(), Settings()), "GET", "api/checks/suites")
    assert status == 501 and err["error"]["code"] == "not_supported"


# baseline import ---------------------------------------------------------------------
BASELINE = {
    "format": "redline-check-baseline",
    "version": 1,
    "suite": {"name": "ci", "headers": {"Authorization": "Bearer ci-secret"}},
    "label": "main",
    "git": {"sha": "abc", "branch": "main", "dirty": False},
    "created_at": 1700000000.0,
    "requests": [
        {
            "name": "list",
            "method": "GET",
            "path": "/orders?api_key=k1",
            "response": {
                "status": 200,
                "content_type": "application/json",
                "headers": {"content-type": "application/json", "set-cookie": "sid=zzz"},
                "body_kind": "json",
                "body": {"items": [], "password": "hunter2"},
            },
        },
        {"name": "page", "response": {"body_kind": "text", "body": "token=abcdef secret"}},
    ],
}


def test_import_baseline(env, store, monkeypatch):
    import redline_core.api_checks as ac

    monkeypatch.setattr(ac, "_engine_baseline", lambda: None)
    api = env()
    status, out = call(api, "POST", "api/checks/baselines/import", BASELINE)
    assert status == 201 and out["requests"] == 2
    status, run = call(api, "GET", f"api/checks/runs/{out['id']}")
    assert run["kind"] == "baseline" and run["status"] == "done" and run["label"] == "main"
    text = json.dumps(run)
    for secret in ("ci-secret", "hunter2", "sid=zzz", "k1", "abcdef"):
        assert secret not in text, secret
    assert run["requests"][0]["response"]["body"]["items"] == []
    rec = make_suite(api)
    status, out = call(
        api, "POST", "api/checks/baselines/import", BASELINE, params={"suite_id": rec["id"]}
    )
    assert store.get_check_run(out["id"])["suite_id"] == rec["id"]
    assert (
        call(api, "POST", "api/checks/baselines/import", BASELINE, params={"suite_id": "x"})[0]
        == 404
    )
    for bad in (
        {**BASELINE, "format": "x"},
        {**BASELINE, "version": 2},
        {**BASELINE, "requests": 1},
    ):
        status, err = call(api, "POST", "api/checks/baselines/import", bad)
        assert status == 400 and err["error"]["code"] == "invalid_baseline"
    huge = json.dumps({**BASELINE, "pad": "x" * (10 * 1024 * 1024)}).encode()
    assert call(api, "POST", "api/checks/baselines/import", huge)[0] == 413


# background control ------------------------------------------------------------------
def test_ui_limits():
    ok = {"repeat": 10, "warmup": 0, "requests": [{"path": "/a"}] * 200}
    control.check_ui_limits(ok)
    with pytest.raises(ValueError, match="200 requests"):
        control.check_ui_limits({"requests": [{"path": "/a"}] * 201})
    with pytest.raises(ValueError, match="5000 calls"):
        control.check_ui_limits({"repeat": 1000, "warmup": 0, "requests": [{"path": "/a"}] * 6})
    with pytest.raises(ValueError, match="GET"):
        control.check_ui_limits({"requests": [{"path": "/a", "method": "DELETE"}]})
    with pytest.raises(ValueError, match="path"):
        control.check_ui_limits({"requests": [{"path": "//evil.example/"}]})
    assert control.planned_calls({"requests": [{}, {"repeat": 5}]}) == 23 + 8


def _fake_engine(monkeypatch, behaviour):
    seen = {}

    def run_suite(suite, *, base_url, kind, baseline=None, run_id=None, suite_id=None,
                  label=None, sink=None, server_metrics=None, on_progress=None, stop=None,
                  transport=None):  # fmt: skip
        seen.update(suite=suite, base_url=base_url, kind=kind, baseline=baseline, stop=stop)
        sink.start({"id": run_id, "suite_id": suite_id, "kind": kind, "baseline_id": None,
                    "label": label, "git": {"sha": "x"}, "created_at": time.time(),
                    "suite": suite})  # fmt: skip
        on_progress({"done": 1, "total": 2, "current": "a"})
        return behaviour(run_id, sink, stop, on_progress)

    monkeypatch.setattr(control, "_engine", lambda: (run_suite, None, None))
    return seen


def test_run_check_done(store, monkeypatch, tmp_path):
    def behaviour(run_id, sink, stop, progress):
        sink.result(run_id, 0, "a", {"name": "a"})
        progress(2, 2)
        sink.finish(run_id, "done", {"ok": True}, None)

    seen = _fake_engine(monkeypatch, behaviour)
    config = {"suite": SUITE, "base_url": "http://127.0.0.1:1", "kind": "baseline", "label": "L"}
    rdir = tmp_path / "runs"
    rdir.mkdir()
    (rdir / "r1.check.json").write_text("{}")
    assert control.run_check(config, store, "r1") == "done"
    run = store.get_check_run("r1")
    assert run["status"] == "done" and run["summary"] == {"ok": True}
    assert run["progress"] == {"done": 2, "total": 2}
    assert run["suite"]["headers"]["Authorization"] == MASK
    assert seen["suite"]["headers"]["Authorization"] == "Bearer s3cret"
    assert not (rdir / "r1.check.json").exists()  # raw headers removed


def test_run_check_compare_loads_baseline(store, monkeypatch):
    store.import_check_baseline({"requests": [{"name": "a"}], "suite": {}}, None)
    base = store.list_check_runs()[0]["id"]
    seen = _fake_engine(monkeypatch, lambda *a: None)  # never finishes the run itself
    config = {"suite": SUITE, "base_url": "http://x", "kind": "compare", "baseline_id": base}
    assert control.run_check(config, store, "c1") == "done"
    assert seen["baseline"]["requests"] == [{"name": "a"}]
    config["baseline_id"] = "missing"
    assert control.run_check(config, store, "c2") == "failed"
    assert "not available" in store.get_check_run("c2")["error"]


def test_run_check_failure_and_timeout(store, monkeypatch, tmp_path):
    def boom(run_id, sink, stop, progress):
        raise RuntimeError("connection refused")

    _fake_engine(monkeypatch, boom)
    config = {"suite": SUITE, "base_url": "http://x", "kind": "baseline"}
    assert control.run_check(config, store, "f1") == "failed"
    assert "connection refused" in store.get_check_run("f1")["error"]

    def slow(run_id, sink, stop, progress):
        while not stop.is_set():
            time.sleep(0.01)
        sink.finish(run_id, "stopped", None, None)

    _fake_engine(monkeypatch, slow)
    config["max_duration_s"] = 0.05
    assert control.run_check(config, store, "t1") == "stopped"
    run = store.get_check_run("t1")
    assert run["status"] == "stopped" and "limited to 15 min" in run["error"]


def test_stop_file_stops_run(store, monkeypatch):
    def wait(run_id, sink, stop, progress):
        control.stop_check(run_id, store.path)
        deadline = time.monotonic() + 5
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        sink.finish(run_id, "stopped", None, None)

    _fake_engine(monkeypatch, wait)
    config = {"suite": SUITE, "base_url": "http://x", "kind": "baseline"}
    assert control.run_check(config, store, "s1") == "stopped"


def test_check_hooks_launch(store, monkeypatch):
    seen = {}

    def fake_launch(suite, base_url, path, **kw):
        seen.update(suite=suite, base_url=base_url, path=path, **kw)
        return kw["run_id"]

    monkeypatch.setattr(control, "launch_check", fake_launch)
    launcher, stopper = control.check_hooks(store, Settings())
    rid = launcher(SUITE, "http://127.0.0.1:8000", kind="baseline", suite_id="s", label="x")
    run = store.get_check_run(rid)
    assert run["status"] == "running" and run["source"] == "ui"
    assert run["base_url"] == "http://127.0.0.1:8000"
    assert run["suite"]["headers"]["Authorization"] == MASK
    assert seen["suite"]["headers"]["Authorization"] == "Bearer s3cret"
    assert seen["path"] == store.path and seen["run_id"] == rid
    assert stopper("bad id!") is False
    with pytest.raises(ValueError):
        launcher({"requests": []}, "http://x", kind="baseline")

    def broken(*a, **kw):
        raise OSError("no python")

    monkeypatch.setattr(control, "launch_check", broken)
    with pytest.raises(OSError):
        launcher(SUITE, "http://x", kind="baseline")
    failed = [r for r in store.list_check_runs() if r["status"] == "failed"]
    assert failed and "could not start" in failed[0]["error"]
    assert control.check_hooks(SQLiteStore(":memory:"), Settings()) == (None, None)


@pytest.mark.skipif(os.name == "nt", reason="file modes")
def test_launch_check_process(store, tmp_path):
    """A real child process: whatever the engine does with an unreachable target, the run
    ends (not left running), its pid/config files go and the log stays private."""
    store.create_check_run({"id": "proc1", "kind": "baseline", "suite": None, "git": None})
    run_id = control.launch_check(
        {"repeat": 1, "warmup": 0, "timeout_s": 1, "requests": [{"path": "/"}]},
        "http://127.0.0.1:9",
        store.path,
        kind="baseline",
        run_id="proc1",
    )
    rdir = tmp_path / "runs"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        run = store.get_check_run(run_id)
        if run["status"] != "running" and not (rdir / f"{run_id}.pid").exists():
            break
        time.sleep(0.1)
    assert store.get_check_run(run_id)["status"] in ("done", "failed")
    assert not (rdir / f"{run_id}.check.json").exists()
    assert (rdir / f"{run_id}.log").stat().st_mode & 0o077 == 0


def test_main_unreadable_config(store, tmp_path):
    store.create_check_run({"id": "m1", "kind": "baseline", "suite": None, "git": None})
    assert control.main(["run", str(tmp_path / "missing.json"), "--store", store.path,
                         "--run-id", "m1"]) == 1  # fmt: skip
    run = store.get_check_run("m1")
    assert run["status"] == "failed" and "cannot read" in run["error"]


def test_draft_uses_older_healthy_trace_when_latest_calls_failed(env, store):
    _seen(store, "/flaky", "/flaky")
    for _ in range(12):  # a broken refactor's calls come after the healthy one
        _seen(store, "/flaky", "/flaky", status=500)
    status, suite = call(env(), "POST", "api/checks/suites/draft", {"routes": ["/flaky"]})
    assert status == 200 and [r["name"] for r in suite["requests"]] == ["GET /flaky"]


def test_ui_compare_with_another_suites_baseline_fails_clearly(store):
    pytest.importorskip("httpx")
    other = dict(BASELINE, requests=[{"name": "elsewhere", "method": "GET", "path": "/x"}])
    store.import_check_baseline(other, None)
    base = store.list_check_runs()[0]["id"]
    config = {
        "suite": SUITE,
        "base_url": "http://127.0.0.1:1",
        "kind": "compare",
        "baseline_id": base,
    }
    assert control.run_check(config, store, "m1") == "failed"
    error = store.get_check_run("m1")["error"]
    assert "baseline main has none of this suite's requests" in error
    assert "different suite" in error
