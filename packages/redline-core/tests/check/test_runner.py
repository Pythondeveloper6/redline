from __future__ import annotations

import json
import threading

import pytest

from redline_core.check import Baseline, Report, Suite, run_suite, schedule
from redline_core.check.runner import CHECK_HEADER, CheckSink, CheckUnreachable, check_header

httpx = pytest.importorskip("httpx")

BASE = "http://app.test"
GIT = {"sha": "abc123", "branch": "main", "dirty": False}


def make_suite(**overrides):
    data = {
        "name": "orders",
        "repeat": 3,
        "warmup": 1,
        "requests": [
            {"name": "list", "path": "/orders"},
            {"name": "one", "path": "/orders/1"},
        ],
    }
    data.update(overrides)
    return Suite.from_dict(data)


class Sink:
    def __init__(self):
        self.events = []

    def start(self, run):
        self.events.append(("start", run))

    def result(self, run_id, idx, name, data):
        self.events.append(("result", idx, name, data))

    def finish(self, run_id, status, summary, error):
        self.events.append(("finish", status, summary, error))


class Metrics:
    def __init__(self, per_idx):
        self.per_idx = per_idx
        self.asked = []

    def for_check(self, run_id, timeout_s=5.0):
        self.asked.append((run_id, timeout_s))
        return self.per_idx


def test_schedule_interleaved_and_warmup():
    s = make_suite(
        requests=[
            {"name": "a", "path": "/a"},
            {"name": "b", "path": "/b", "perf": False},
            {"name": "c", "path": "/c"},
        ],
        repeat=3,
        warmup=2,
    )
    assert schedule(s) == [
        (0, 0), (1, 0), (2, 0),
        (0, 0), (1, 0), (2, 0),
        (0, 1), (1, 1), (2, 1),
        (0, 2), (2, 2),
        (0, 3), (1, 3), (2, 3),
    ]  # fmt: skip


def test_baseline_run_interleaves_and_tags(app, wsgi):
    app.json("/orders", {"items": [1, 2]})
    app.json("/orders/1", {"id": 1})
    sink = Sink()
    result = run_suite(
        make_suite(),
        base_url=BASE,
        kind="baseline",
        run_id="r1",
        sink=sink,
        transport=wsgi,
        git=GIT,
        label="before",
    )
    assert isinstance(result, Baseline)
    assert [c[1] for c in app.calls] == ["/orders", "/orders/1"] * 4
    assert [c[2] for c in app.calls] == [
        "r1;req=0;n=0", "r1;req=1;n=0",
        "r1;req=0;n=1", "r1;req=1;n=1",
        "r1;req=0;n=2", "r1;req=1;n=2",
        "r1;req=0;n=3", "r1;req=1;n=3",
    ]  # fmt: skip
    entry = result.request("list")
    assert entry["calls"] == 3 and entry["failed"] == 0 and not entry["broken"]
    assert len(entry["perf"]["samples_ms"]) == 3  # warmup not timed
    assert entry["perf"]["median"] is not None and entry["statuses"] == {"200": 3}
    assert entry["response"]["body"] == {"items": [1, 2]}
    assert entry["volatile"] == [] and entry["server"] is None
    assert result.id == "r1" and result.label == "before" and result.git == GIT
    kinds = [e[0] for e in sink.events]
    assert kinds == ["start", "result", "result", "finish"]
    start = sink.events[0][1]
    assert set(start) == {
        "id",
        "suite_id",
        "kind",
        "baseline_id",
        "label",
        "git",
        "created_at",
        "suite",
    }
    assert start["kind"] == "baseline" and start["suite"]["name"] == "orders"
    assert sink.events[-1][1] == "done" and sink.events[-1][2]["requests"] == 2
    assert sink.events[1][1:3] == (0, "list")
    assert check_header("x", 2, 5) == "x;req=2;n=5" and CHECK_HEADER == "X-Redline-Check"


def test_warmup_is_not_timed(app, wsgi):
    import time

    state = {"n": 0}

    def slow_first(env):
        state["n"] += 1
        if state["n"] == 1:
            time.sleep(0.3)
        return 200, {"Content-Type": "text/plain"}, b"ok"

    app.routes["/a"] = slow_first
    s = make_suite(requests=[{"path": "/a"}], repeat=4, warmup=1)
    b = run_suite(s, base_url=BASE, kind="baseline", transport=wsgi, git=GIT)
    samples = b.requests[0]["perf"]["samples_ms"]
    assert len(samples) == 4 and max(samples) < 250


def test_volatile_fields_detected(app, wsgi):
    counter = {"n": 0}

    def ticking(env):
        counter["n"] += 1
        body = json.dumps({"id": 1, "now": counter["n"], "meta": {"rid": f"r{counter['n']}"}})
        return (
            200,
            {
                "Content-Type": "application/json",
                "ETag": str(counter["n"]),
                "Cache-Control": f"c{counter['n']}",
            },
            body.encode(),
        )

    app.routes["/t"] = ticking
    b = run_suite(
        make_suite(requests=[{"path": "/t"}]),
        base_url=BASE,
        kind="baseline",
        transport=wsgi,
        git=GIT,
    )
    e = b.requests[0]
    assert e["volatile"] == ["now", "meta.rid"]
    assert e["volatile_headers"] == ["cache-control"]


def test_perf_false_only_first_and_last_round(app, wsgi):
    app.json("/a", {})
    app.json("/b", {})
    s = make_suite(requests=[{"path": "/a"}, {"path": "/b", "perf": False}], repeat=5, warmup=0)
    b = run_suite(s, base_url=BASE, kind="baseline", transport=wsgi, git=GIT)
    assert [c[1] for c in app.calls].count("/b") == 2
    assert b.request("GET /b")["perf"] is None and b.request("GET /b")["calls"] == 2


def test_env_headers_sent_but_not_stored(app, wsgi):
    seen = []

    def echo(env):
        seen.append(env.get("HTTP_AUTHORIZATION"))
        return 200, {"Content-Type": "application/json"}, b"{}"

    app.routes["/a"] = echo
    s = make_suite(
        headers={"Authorization": "Bearer ${env:TOKEN}"},
        requests=[{"path": "/a"}],
        repeat=1,
        warmup=0,
    )
    sink = Sink()
    b = run_suite(
        s,
        base_url=BASE,
        kind="baseline",
        transport=wsgi,
        environ={"TOKEN": "s3cret"},
        sink=sink,
        git=GIT,
    )
    assert seen == ["Bearer s3cret"]
    dumped = json.dumps(b.to_dict()) + json.dumps([e[1:] for e in sink.events], default=str)
    assert "s3cret" not in dumped and "${env:TOKEN}" in dumped
    from redline_core.check.suite import SuiteError

    with pytest.raises(SuiteError, match="TOKEN is not set"):
        run_suite(s, base_url=BASE, kind="baseline", transport=wsgi, environ={}, git=GIT)


def test_failures_and_broken():
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/flaky":
            calls["n"] += 1
        if request.url.path == "/flaky" and calls["n"] == 2:  # 1 of 4 fails: not broken
            raise httpx.ReadTimeout("slow", request=request)
        if request.url.path == "/down":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True})

    s = make_suite(
        requests=[{"path": "/ok"}, {"path": "/flaky"}, {"path": "/down"}], repeat=4, warmup=0
    )
    b = run_suite(
        s, base_url=BASE, kind="baseline", transport=httpx.MockTransport(handler), git=GIT
    )
    down = b.request("GET /down")
    assert down["failed"] == 4 and down["broken"] and down["response"] is None
    assert down["errors"] == {"ReadTimeout": 4} and down["perf"]["samples_ms"] == []
    flaky = b.request("GET /flaky")
    assert 0 < flaky["failed"] < 4
    assert not b.request("GET /ok")["broken"]
    assert b.summary()["broken"] == 1


def test_stale_keepalive_connection_is_retried_once():
    # uvicorn resets the connection after a 500: the next call on it must not count as failed.
    state = {"reset": False}

    def handler(request):
        if request.url.path == "/boom":
            state["reset"] = True
            return httpx.Response(500)
        if state["reset"]:
            state["reset"] = False
            raise httpx.ReadError("Connection reset by peer", request=request)
        return httpx.Response(200, json={"ok": True})

    s = make_suite(requests=[{"path": "/boom"}, {"path": "/ok"}], repeat=3, warmup=1)
    b = run_suite(
        s, base_url=BASE, kind="baseline", transport=httpx.MockTransport(handler), git=GIT
    )
    ok = b.request("GET /ok")
    assert ok["failed"] == 0 and len(ok["perf"]["samples_ms"]) == 3


def test_unreachable_raises_and_finishes_failed():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    sink = Sink()
    with pytest.raises(CheckUnreachable, match="could not reach"):
        run_suite(
            make_suite(),
            base_url=BASE,
            kind="baseline",
            transport=httpx.MockTransport(handler),
            sink=sink,
            git=GIT,
        )
    assert sink.events[-1][0] == "finish" and sink.events[-1][1] == "failed"
    assert "could not reach" in sink.events[-1][3]


def test_stop_event_and_ctrl_c(app, wsgi):
    app.json("/orders", {})
    app.json("/orders/1", {})
    stop = threading.Event()

    def progress(ev):
        if ev["done"] == 3:
            stop.set()

    sink = Sink()
    b = run_suite(
        make_suite(),
        base_url=BASE,
        kind="baseline",
        transport=wsgi,
        stop=stop,
        on_progress=progress,
        sink=sink,
        git=GIT,
    )
    assert b.status == "stopped" and len(app.calls) == 3
    assert sink.events[-1][1] == "stopped"

    def interrupt(ev):
        if ev["done"] == 2:
            raise KeyboardInterrupt

    app.calls.clear()
    sink = Sink()
    b = run_suite(
        make_suite(),
        base_url=BASE,
        kind="baseline",
        transport=wsgi,
        on_progress=interrupt,
        sink=sink,
        git=GIT,
    )
    assert b.status == "stopped" and len(app.calls) == 2
    assert sink.events[-1][1] == "stopped"
    assert "Ctrl-C" in sink.events[-1][2]["stopped_reason"]


def test_progress_events(app, wsgi):
    app.json("/orders", {})
    app.json("/orders/1", {})
    events = []
    run_suite(
        make_suite(),
        base_url=BASE,
        kind="baseline",
        transport=wsgi,
        on_progress=events.append,
        git=GIT,
    )
    assert [e["done"] for e in events] == list(range(1, 9))
    assert {e["total"] for e in events} == {8}
    assert [e["phase"] for e in events[:2]] == ["warmup", "warmup"]
    assert events[2]["phase"] == "timed" and events[2]["status"] == 200
    assert [e["current"] for e in events[:2]] == ["list", "one"]


def test_sink_errors_never_stop_a_run(app, wsgi):
    app.json("/orders", {})
    app.json("/orders/1", {})

    class Broken:
        def start(self, run):
            raise RuntimeError("db down")

        result = finish = start

    b = run_suite(
        make_suite(), base_url=BASE, kind="baseline", transport=wsgi, sink=Broken(), git=GIT
    )
    assert b.status == "done"
    assert isinstance(Sink(), CheckSink)


def test_compare_end_to_end(app, wsgi):
    app.json("/orders", {"items": [1, 2], "now": 1})
    app.json("/orders/1", {"id": 1, "name": "a"})
    suite = make_suite(repeat=5)
    q1 = {"count": 3, "sql": "SELECT 1"}
    one_q = {"queries_median": 1, "queries_max": 1, "sql": {}, "issues": []}
    metrics = Metrics(
        {0: {"queries_median": 3, "queries_max": 3, "sql": {"f1": q1}, "issues": []}, 1: one_q}
    )
    baseline = run_suite(
        suite,
        base_url=BASE,
        kind="baseline",
        transport=wsgi,
        server_metrics=metrics,
        git=GIT,
        run_id="b1",
    )
    assert baseline.request("list")["server"]["queries_median"] == 3
    assert metrics.asked == [("b1", 5.0)]

    app.json("/orders/1", {"id": 1, "name": "b"})
    n1 = {"kind": "n_plus_one", "fingerprint": "f2", "title": "N+1"}
    sql_after = {"f1": q1, "f2": {"count": 6, "sql": "SELECT 2"}}
    after = Metrics(
        {0: {"queries_median": 9, "queries_max": 9, "sql": sql_after, "issues": [n1]}, 1: one_q}
    )
    sink = Sink()
    report = run_suite(
        suite,
        base_url=BASE,
        kind="compare",
        baseline=baseline,
        transport=wsgi,
        server_metrics=after,
        sink=sink,
        git=GIT,
        run_id="c1",
    )
    assert isinstance(report, Report)
    lst, one = report.requests
    assert lst["perf"]["verdict"] == "slower" and lst["perf"]["reason"] in (
        "queries",
        "time+queries",
    )
    assert lst["server"]["queries_change"] == 6
    assert [i["kind"] for i in lst["server"]["new_issues"]] == ["n_plus_one"]
    assert [s["fingerprint"] for s in lst["server"]["new_sql"]] == ["f2"]
    assert lst["response"]["verdict"] == "same"
    assert one["response"]["verdict"] == "changed"
    assert one["response"]["diff"] == [
        {"path": "name", "kind": "changed", "before": "a", "after": "b"}
    ]
    assert report.summary["queries_change"] == 6 and report.summary["server_side"]
    assert report.baseline["id"] == "b1" and report.run["id"] == "c1"
    start = sink.events[0][1]
    assert start["kind"] == "compare" and start["baseline_id"] == "b1"
    assert sink.events[-1][1] == "done" and sink.events[-1][2]["changed"] == 1
    assert [e[2] for e in sink.events if e[0] == "result"] == ["list", "one"]
    assert report.exit_code() == 1
    json.dumps(report.to_dict())


def test_compare_baseline_round_trip_through_file(app, wsgi, tmp_path):
    app.json("/orders", {"token": "s3cret", "v": 1})
    app.json("/orders/1", {"id": 1})
    suite = make_suite()
    b = run_suite(suite, base_url=BASE, kind="baseline", transport=wsgi, git=GIT)
    path = b.write(tmp_path / "b.json")
    assert "s3cret" not in path.read_text()
    loaded = Baseline.from_file(path)
    report = run_suite(
        suite, base_url=BASE, kind="compare", baseline=loaded, transport=wsgi, git=GIT
    )
    # the live body is redacted the same way, so the masked secret is not a difference
    assert [r["response"]["verdict"] for r in report.requests] == ["same", "same"]
    assert report.baseline["file"] == str(path)


def test_compare_suite_changes(app, wsgi):
    app.json("/orders", {})
    app.json("/orders/1", {})
    app.json("/new", {})
    b = run_suite(make_suite(), base_url=BASE, kind="baseline", transport=wsgi, git=GIT)
    s2 = make_suite(requests=[{"name": "list", "path": "/orders"}, {"name": "new", "path": "/new"}])
    report = run_suite(s2, base_url=BASE, kind="compare", baseline=b, transport=wsgi, git=GIT)
    new = report.requests[1]
    assert new["perf"]["verdict"] == "n/a" and new["response"]["verdict"] == "n/a"
    assert report.summary["not_in_baseline"] == ["new"]
    assert report.summary["not_in_suite"] == ["one"]


def test_bad_arguments():
    with pytest.raises(ValueError, match="needs a baseline"):
        run_suite(make_suite(), base_url=BASE, kind="compare")
    with pytest.raises(ValueError, match="kind"):
        run_suite(make_suite(), base_url=BASE, kind="nope")  # type: ignore[arg-type]
