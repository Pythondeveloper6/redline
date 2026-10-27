"""v0.2 JSON API routes (CONTRACT §12)."""

from __future__ import annotations

import base64
import json
import re
import time

import pytest

from redline_core import ui
from redline_core.api import RawResponse, RedlineAPI, snapshot_html
from redline_core.conf import Settings
from redline_core.detect import detect
from redline_core.histogram import Histogram
from redline_core.runs import Run, RunStep

from .conftest import make_callsite, make_trace, sql_span
from .test_storage_v2 import cache_span, http_span, n1_trace


@pytest.fixture
def api(store):
    return RedlineAPI(store, Settings(), framework="django")


def ok(result, status=200):
    code, payload = result
    assert code == status, payload
    json.dumps(payload)
    return payload


def err(result, status, code):
    got_status, payload = result
    assert (got_status, payload["error"]["code"]) == (status, code), payload
    return payload


# traces ------------------------------------------------------------------------
def test_trace_filters(api, store):
    now = time.time()
    a = n1_trace(started_at=now - 30)
    b = make_trace(started_at=now - 10, tags={"replay_of": a.id})
    store.save(a)
    store.save(b)
    ids = lambda p: [i["id"] for i in ok(api.dispatch("GET", "api/traces", p))["items"]]  # noqa: E731
    assert ids({"sql": "items"}) == [a.id]
    assert ids({"since": str(now - 20)}) == [b.id]
    assert ids({"replay_of": a.id}) == [b.id]
    err(api.dispatch("GET", "api/traces", {"since": "soon"}), 400, "invalid_filter")


def test_poll(api, store):
    now = time.time()
    t1, t2 = make_trace(started_at=now - 2), make_trace(started_at=now - 1)
    store.save(t1)
    store.save(t2)
    assert [s["id"] for s in api.poll(now - 5)] == [t1.id, t2.id]
    assert [s["id"] for s in api.poll(t1.started_at, t1.id)] == [t2.id]
    assert api.poll(now) == []

    class Legacy:  # no traces_since: falls back to a ``since`` list
        def __init__(self, inner):
            self.inner = inner

        def list(self, *a, **k):
            return self.inner.list(*a, **k)

    assert [s["id"] for s in RedlineAPI(Legacy(store)).poll(now - 5)] == [t1.id, t2.id]


def test_compare_traces(api, store):
    cs = make_callsite()
    a = make_trace(
        spans=[sql_span("SELECT * FROM t WHERE id = %s", [i], callsite=cs) for i in range(5)]
    )
    a.issues = detect(a)
    b = make_trace(
        spans=[
            sql_span("SELECT * FROM t WHERE id = %s", [1], duration_ms=4),
            http_span("https://x.io/a", 200, 50),
        ],
        duration_ms=70,
    )
    store.save(a)
    store.save(b)
    data = ok(api.dispatch("GET", "api/compare", {"a": a.id, "b": b.id}))
    assert data["a"]["id"] == a.id and data["b"]["id"] == b.id
    assert data["delta"] == {"duration_ms": 60.0, "query_count": -4, "query_ms": -1.0}
    assert data["by_kind"]["sql"] == {"a_ms": 5.0, "b_ms": 4.0, "a_count": 5, "b_count": 1}
    assert data["by_kind"]["http_out"]["b_count"] == 1
    [row] = data["sql"]
    assert (row["a_count"], row["b_count"], row["a_ms"], row["b_ms"]) == (5, 1, 5.0, 4.0)
    assert re.fullmatch(r"[0-9a-f]{16}", row["fingerprint"]) and "?" in row["sql"]
    assert [i["kind"] for i in data["issues"]["only_a"]] == ["n_plus_one"]
    assert data["issues"]["only_b"] == [] and data["issues"]["both"] == []
    err(api.dispatch("GET", "api/compare", {"a": a.id}), 400, "invalid_filter")
    err(api.dispatch("GET", "api/compare", {"a": a.id, "b": "nope"}), 404, "trace_not_found")


# analytics ---------------------------------------------------------------------
def test_sql_cache_external(api, store):
    t = n1_trace()
    t.spans += [http_span("https://x.io/u/1"), cache_span("get", "k:1", hit=True)]
    store.save(t)
    [item] = ok(api.dispatch("GET", "api/sql", {"q": "items", "window": "1h"}))["items"]
    assert item["count"] == 6 and item["sample_trace_id"] == t.id
    assert ok(api.dispatch("GET", "api/sql", {"q": "zzz"}))["items"] == []
    [c] = ok(api.dispatch("GET", "api/cache"))["items"]
    assert set(c) >= {
        "backend",
        "key_pattern",
        "gets",
        "hits",
        "misses",
        "hit_rate",
        "sets",
        "unread_sets",
        "stampedes",
    }
    [e] = ok(api.dispatch("GET", "api/external", {"window": "7d"}))["items"]
    assert set(e) >= {
        "method",
        "host",
        "url_pattern",
        "count",
        "p95_ms",
        "max_ms",
        "errors",
        "error_rate",
        "duplicates",
        "retries",
    }
    for path in ("api/sql", "api/cache", "api/external", "api/history"):
        err(api.dispatch("GET", path, {"window": "forever"}), 400, "invalid_window")
        err(api.dispatch("POST", path), 405, "method_not_allowed")
    indexes = ok(api.dispatch("GET", "api/indexes"))
    assert {"candidates", "unused", "unused_supported"} <= set(indexes)


def test_history_and_releases(api, store):
    now = time.time()
    for rel, at in (("v1", now - 3 * 86_400), ("v2", now - 60)):
        t = make_trace(started_at=at)
        t.release = rel
        store.save(t)
    data = ok(api.dispatch("GET", "api/history"))  # 30d, daily buckets
    assert sum(p["count"] for p in data["series"]) == 2
    assert all(p["t"] % 86_400 == 0 for p in data["series"])
    assert [r["release"] for r in data["releases"]] == ["v1", "v2"]
    hourly = ok(api.dispatch("GET", "api/history", {"window": "24h"}))
    assert [p["count"] for p in hourly["series"]] == [1]
    route = {"route": "/orders/<int:pk>/", "method": "get", "bucket": "1h", "window": "7d"}
    assert len(ok(api.dispatch("GET", "api/history", route))["series"]) == 2
    assert ok(api.dispatch("GET", "api/history", {"route": "/none/"}))["series"] == []
    err(api.dispatch("GET", "api/history", {"bucket": "1w"}), 400, "invalid_filter")
    items = ok(api.dispatch("GET", "api/releases"))["items"]
    assert [(r["release"], r["requests"]) for r in items] == [("v2", 1), ("v1", 1)]


def test_history_compare(api, store):
    now = time.time()
    for rel, ms in (("v1", 10), ("v2", 100)):
        t = make_trace(duration_ms=ms, started_at=now - 10)
        t.release = rel
        store.save(t)
    data = ok(api.dispatch("GET", "api/history/compare", {"a": "release:v1", "b": "release:v2"}))
    assert [e["verdict"] for e in data["endpoints"]] == ["regressed"]
    assert set(data["issues"]) == {"new", "resolved", "persisting"}
    rng = f"range:{int(now - 3600)}-{int(now + 60)}"
    data = ok(api.dispatch("GET", "api/history/compare", {"a": rng, "b": rng}))
    assert data["endpoints"][0]["verdict"] == "same"
    for a, b in (("v1", "release:v2"), ("release:v1", None), ("range:9-1", "release:v1")):
        err(api.dispatch("GET", "api/history/compare", {"a": a, "b": b}), 400, "invalid_period")


# runs ------------------------------------------------------------------------------
def test_runs_read_and_delete(api, store):
    assert ok(api.dispatch("GET", "api/runs")) == {"items": []}
    run = Run(name="smoke", status="running")
    run.steps.append(RunStep(step=0, target_rps=5, sent=10, ok=10, hist=Histogram([3, 4])))
    store.save_run(run)
    store.save(n1_trace(tags={"run_id": run.id, "step": "0"}))
    [item] = ok(api.dispatch("GET", "api/runs"))["items"]
    assert item["id"] == run.id and item["status"] == "running" and len(item["steps"]) == 1
    detail = ok(api.dispatch("GET", f"api/runs/{run.id}"))
    assert detail["steps"][0]["p50_ms"] > 0 and "summary" in detail
    b = ok(api.dispatch("GET", f"api/runs/{run.id}/bottlenecks"))
    assert b["steps"] == [{"step": 0, "target_rps": 5}] and len(b["items"]) == 2
    err(api.dispatch("DELETE", f"api/runs/{run.id}"), 409, "run_active")
    store.finish_run(run.id, "stopped")
    assert ok(api.dispatch("DELETE", f"api/runs/{run.id}")) == {"deleted": 1}
    for method, path in (
        ("GET", "api/runs/nope"),
        ("DELETE", "api/runs/nope"),
        ("GET", "api/runs/nope/bottlenecks"),
    ):
        err(api.dispatch(method, path), 404, "run_not_found")
    err(api.dispatch("PUT", "api/runs"), 405, "method_not_allowed")
    err(api.dispatch("POST", f"api/runs/{run.id}"), 405, "method_not_allowed")
    err(api.dispatch("GET", f"api/runs/{run.id}/stop"), 405, "method_not_allowed")
    err(api.dispatch("GET", "api/runs", {"limit": "x"}), 400, "invalid_filter")


RUN_BODY = {
    "name": "checkout",
    "requests": [{"method": "get", "path": "/orders/", "weight": 2}],
    "profile": {
        "mode": "steps",
        "start_rps": 5,
        "end_rps": 20,
        "step_rps": 5,
        "duration_s": 60,
        "step_duration_s": 15,
    },
}


def test_start_run(store):
    err(RedlineAPI(store).dispatch("POST", "api/runs", body=RUN_BODY), 403, "load_disabled")
    enabled = Settings(load_from_ui=True)
    err(
        RedlineAPI(store, enabled).dispatch("POST", "api/runs", body=RUN_BODY), 501, "not_supported"
    )
    calls = []

    def launcher(config, base_url):
        calls.append((config, base_url))
        if config["name"] == "boom":
            raise ValueError("cannot reach the app")
        return "run123"

    api = RedlineAPI(store, enabled, run_launcher=launcher)
    data = ok(
        api.dispatch("POST", "api/runs", body=json.dumps(RUN_BODY), base_url="http://app:8000/"),
        202,
    )
    assert data == {"id": "run123", "name": "checkout", "status": "running"}
    config, base = calls[0]
    assert base == "http://app:8000/" and config["requests"][0]["method"] == "GET"
    assert config["profile"]["mode"] == "steps"
    err(api.dispatch("POST", "api/runs", body={**RUN_BODY, "name": "boom"}), 400, "invalid_body")
    err(api.dispatch("POST", "api/runs", body=b"{nope"), 400, "invalid_json")
    bad_bodies = [
        [],
        {**RUN_BODY, "name": ""},
        {**RUN_BODY, "requests": []},
        {**RUN_BODY, "requests": ["GET /"]},
        {**RUN_BODY, "requests": [{"method": "GET", "path": "http://evil/"}]},
        {**RUN_BODY, "requests": [{"method": "GET", "path": "//evil/"}]},
        {**RUN_BODY, "requests": [{"method": "G E T", "path": "/"}]},
        {**RUN_BODY, "requests": [{"method": "GET", "path": "/", "weight": 0}]},
        {**RUN_BODY, "profile": {"mode": "spike", "duration_s": 1}},
        {**RUN_BODY, "profile": {"mode": "constant", "rps": 5}},
        {**RUN_BODY, "profile": {"mode": "constant", "rps": True, "duration_s": 5}},
        {**RUN_BODY, "profile": {"mode": "ramp", "start_rps": 1, "duration_s": 5}},
    ]
    for body in bad_bodies:
        err(api.dispatch("POST", "api/runs", body=body), 400, "invalid_body")
    assert len(calls) == 2


def test_stop_run(store):
    run = Run(name="r")
    store.save_run(run)
    err(RedlineAPI(store).dispatch("POST", f"api/runs/{run.id}/stop"), 501, "not_supported")
    stopped = []
    api = RedlineAPI(store, run_stopper=lambda rid: stopped.append(rid) or True)
    assert ok(api.dispatch("POST", f"api/runs/{run.id}/stop")) == {"stopped": True}
    assert stopped == [run.id]
    err(api.dispatch("POST", "api/runs/nope/stop"), 404, "run_not_found")


def test_store_without_v02_methods_answers_501(store):
    class Minimal:
        path = "x"

        def count(self):
            return 0

    class Raising(Minimal):
        def search_sql(self, *a):
            raise NotImplementedError

        def get_run(self, run_id):
            raise NotImplementedError

    for s in (Minimal(), Raising()):
        api = RedlineAPI(s, Settings(load_from_ui=True), run_stopper=lambda rid: True)
        for method, path in (
            ("GET", "api/sql"),
            ("GET", "api/cache"),
            ("GET", "api/external"),
            ("GET", "api/history"),
            ("GET", "api/releases"),
            ("GET", "api/runs"),
            ("GET", "api/runs/x"),
            ("GET", "api/runs/x/bottlenecks"),
            ("DELETE", "api/runs/x"),
        ):
            err(api.dispatch(method, path), 501, "not_supported")
        err(
            api.dispatch("GET", "api/history/compare", {"a": "release:a", "b": "release:b"}),
            501,
            "not_supported",
        )
        assert ok(api.dispatch("POST", "api/runs/x/stop")) == {"stopped": True}


# snapshot ------------------------------------------------------------------------
def _decode_imports(page: str) -> dict[str, str]:
    m = re.search(r'<script type="importmap">(.*?)</script>', page, re.S)
    imports = json.loads(m.group(1))["imports"]
    return {k: base64.b64decode(v.split(",", 1)[1]).decode("utf-8") for k, v in imports.items()}


def test_snapshot(api, store):
    t = n1_trace(profile={"profiles": [], "shared": {"frames": []}})
    t.request["body"] = '</script><script>alert("x")</script>   <!--'
    store.save(t)
    result = api.dispatch("GET", f"api/traces/{t.id}/snapshot")
    assert isinstance(result, RawResponse)
    status, payload = result  # unpacks like every other response
    assert status == 200 and payload is result
    assert result.content_type == "text/html; charset=utf-8"
    assert result.headers == {"Content-Disposition": f'attachment; filename="redline-{t.id}.html"'}
    page = result.body.decode("utf-8")
    assert page.count("</script>") == page.count("<script")  # nothing broke out early
    m = re.search(r"<script>window.__REDLINE_SNAPSHOT__ = (.*?);</script>", page, re.S)
    snap = json.loads(m.group(1))
    assert snap["trace"]["id"] == t.id and snap["trace"]["has_profile"] is True
    assert snap["trace"]["request"]["body"] == t.request["body"]
    assert snap["profile"] == t.profile
    err(api.dispatch("GET", "api/traces/nope/snapshot"), 404, "trace_not_found")
    err(api.dispatch("DELETE", f"api/traces/{t.id}/snapshot"), 405, "method_not_allowed")


@pytest.mark.skipif(not ui.is_built(), reason="UI bundle not built")
def test_snapshot_inlines_the_real_bundle():
    page = snapshot_html({"trace": {"id": "x"}, "profile": None})
    assert 'src="./assets' not in page and 'href="./assets' not in page
    assert "<style>" in page
    # the global is set by a classic script before the import map and the app module
    g = page.index("window.__REDLINE_SNAPSHOT__")
    assert g < page.index('<script type="importmap">') < page.index('<script type="module">')
    modules = _decode_imports(page)
    chunks = {p.name for p in (ui.static_dir() / "assets").glob("*.js")}
    assert {k.removeprefix("@redline/") for k in modules} == chunks  # lazy chunks included
    for text in modules.values():
        assert not re.search(r"""["']\./[\w.-]+\.js["']""", text)  # all rewritten
    entry = re.search(r'<script type="module">import "(@redline/[^"]+)";</script>', page)
    assert entry.group(1) in modules


def test_snapshot_with_synthetic_bundle(tmp_path, monkeypatch):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(
        '<html><head><script type="module" crossorigin src="./assets/main.js"></script>'
        '<link rel="modulepreload" href="./assets/lazy.js">'
        '<link rel="stylesheet" href="./assets/app.css"></head><body></body></html>'
    )
    (static / "assets" / "main.js").write_text('import("./lazy.js");export const a=1;')
    (static / "assets" / "lazy.js").write_text('import{a}from"./main.js";const u="./x.js";')
    (static / "assets" / "app.css").write_text(
        "body{background:url(./bg.svg)}/*</style>*/a{b:url(./missing.png)}"
    )
    (static / "assets" / "bg.svg").write_text("<svg/>")
    monkeypatch.setattr(ui, "_STATIC", static.resolve())
    page = snapshot_html({"trace": None})
    modules = _decode_imports(page)
    assert modules["@redline/main.js"] == 'import("@redline/lazy.js");export const a=1;'
    assert modules["@redline/lazy.js"] == 'import{a}from"@redline/main.js";const u="./x.js";'
    assert "modulepreload" not in page
    assert "url(data:image/svg+xml;base64," in page and "url(./missing.png)" in page
    assert "<\\/style>" in page
    assert '<script type="module">import "@redline/main.js";</script>' in page

    (static / "index.html").unlink()  # no bundle: the placeholder page still carries the data
    page = snapshot_html({"trace": {"id": "y"}})
    assert "window.__REDLINE_SNAPSHOT__" in page and "importmap" not in page
