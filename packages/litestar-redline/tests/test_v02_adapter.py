"""v0.2 adapter features: Server-Timing, the badge, SSE ``api/stream``, the snapshot
passthrough, ``POST api/runs`` and event-loop blocking tracking."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from litestar import Litestar, MediaType, Response, get
from litestar.response import File, Stream
from litestar.testing import TestClient

from litestar_redline import RedlinePlugin
from redline_core import stream
from redline_core.load import control

ROOT = Path(__file__).resolve().parent.parent
PAGE = "<html><head><title>t</title></head><body><h1>hi</h1></body></html>"


@get("/page", media_type=MediaType.HTML)
async def page() -> str:
    return PAGE


@get("/fragment", media_type=MediaType.HTML)
async def fragment() -> str:
    return "<p>no body tag</p>"


@get("/csp")
async def csp() -> Response[str]:
    return Response(
        PAGE, media_type=MediaType.HTML, headers={"Content-Security-Policy": "script-src 'none'"}
    )


@get("/timed")
async def timed() -> Response[str]:
    return Response(PAGE, media_type=MediaType.HTML, headers={"Server-Timing": "app;dur=1.0"})


@get("/missing", media_type=MediaType.HTML)
async def missing() -> Response[str]:
    return Response(PAGE, media_type=MediaType.HTML, status_code=404)


@get("/json")
async def as_json() -> dict:
    return {"html": "</body>"}


@get("/html-stream")
async def html_stream() -> Stream:
    async def gen():
        yield b"<html><body>"
        yield b"streamed</body></html>"

    return Stream(gen(), media_type=MediaType.HTML)


@get("/blocking")
async def blocking() -> dict:
    time.sleep(0.08)  # blocks the event loop on purpose
    return {"ok": True}


def _file_handler(path: Path):
    @get("/file")
    async def a_file() -> File:
        return File(path, media_type=MediaType.HTML)

    return a_file


@pytest.fixture
def make_app(tmp_path):
    plugins = []

    def _make(*, debug: bool = True, settings: dict | None = None):
        html_file = tmp_path / "f.html"
        html_file.write_text(PAGE)
        plugin = RedlinePlugin(
            settings={
                "storage_path": str(tmp_path / f"r{len(plugins)}.db"),
                "project_root": str(ROOT),
                **(settings or {}),
            },
            guards=[],
        )
        plugins.append(plugin)
        handlers = [page, fragment, csp, timed, missing, as_json, html_stream, blocking,
                    _file_handler(html_file)]  # fmt: skip
        app = Litestar(route_handlers=handlers, plugins=[plugin], debug=debug)
        return app, plugin

    yield _make
    for p in plugins:
        p.close()


def _client(app) -> TestClient:
    return TestClient(app, base_url="http://localhost")


# ---------------------------------------------------------------------------- Server-Timing
def test_server_timing_header(make_app):
    app, _ = make_app()
    with _client(app) as c:
        r = c.get("/json")
        assert r.headers["server-timing"].startswith("total;dur=")
        assert 'redline;desc="' in r.headers["server-timing"]
        r = c.get("/timed")
        assert r.headers["server-timing"].startswith("app;dur=1.0, total;dur=")
        assert "server-timing" not in c.get("/_redline/api/meta").headers


def test_server_timing_off(make_app):
    app, _ = make_app(settings={"server_timing": False})
    with _client(app) as c:
        assert "server-timing" not in c.get("/json").headers


def test_server_timing_auto_needs_debug(make_app):
    app, _ = make_app(debug=False)  # "auto" (the default) is a side channel: debug only
    with _client(app) as c:
        assert "server-timing" not in c.get("/json").headers
    app, _ = make_app(debug=False, settings={"server_timing": True})
    with _client(app) as c:
        assert "server-timing" in c.get("/json").headers


# ---------------------------------------------------------------------------- badge
def test_badge_injected_into_html(make_app):
    app, plugin = make_app()
    with _client(app) as c:
        r = c.get("/page")
        assert r.status_code == 200
        assert '<script src="/_redline/badge.js" defer' in r.text
        assert r.text.index("badge.js") < r.text.index("</body>")
        assert int(r.headers["content-length"]) == len(r.content)
        trace_id = r.text.split('data-redline-trace="', 1)[1].split('"', 1)[0]
        assert f'redline;desc="{trace_id}"' in r.headers["server-timing"]
        plugin.store.flush(5)
        assert plugin.store.get(trace_id) is not None


@pytest.mark.parametrize("url", ["/fragment", "/csp", "/json", "/html-stream", "/file", "/missing"])
def test_badge_not_injected(make_app, url):
    app, _ = make_app()
    with _client(app) as c:
        r = c.get(url)
        assert "badge.js" not in r.text
        if url == "/html-stream":
            assert r.text == "<html><body>streamed</body></html>"
        if url == "/file":
            assert r.text == PAGE


def test_badge_not_injected_when_not_recording(make_app, monkeypatch):
    """An inert builder (Redline turned off between the gate and ``start_trace``)."""
    app, plugin = make_app()
    calls = []

    def ignored(path):  # the middleware's gate says "record", start_trace says "inert"
        calls.append(path)
        return len(calls) % 2 == 0

    monkeypatch.setattr(plugin.recorder.sampler, "ignored", ignored)
    with _client(app) as c:
        r = c.get("/page")
        assert r.status_code == 200 and "badge.js" not in r.text
        assert int(r.headers["content-length"]) == len(r.content)


def test_badge_not_on_redline_pages(make_app):
    app, _ = make_app()
    with _client(app) as c:
        assert "data-redline-trace" not in c.get("/_redline/").text


def test_badge_follows_debug_and_setting(make_app):
    app, _ = make_app(debug=False)
    with _client(app) as c:
        assert "badge.js" not in c.get("/page").text
    app, _ = make_app(debug=False, settings={"badge": True})
    with _client(app) as c:
        assert "badge.js" in c.get("/page").text
    app, _ = make_app(debug=True, settings={"badge": False})
    with _client(app) as c:
        assert "badge.js" not in c.get("/page").text


def test_top_level_static_files_are_served(make_app, monkeypatch, tmp_path):
    from redline_core import ui

    js = tmp_path / "badge.js"
    js.write_text("/* badge */")
    monkeypatch.setattr(ui, "asset_path", lambda rel: js if rel == "badge.js" else None)
    app, _ = make_app()
    with _client(app) as c:
        r = c.get("/_redline/badge.js")
        assert r.status_code == 200 and r.text == "/* badge */"
        assert r.headers["content-type"].startswith("text/javascript")


# ---------------------------------------------------------------------------- SSE
def test_sse_stream_sends_trace_and_ping(make_app, monkeypatch):
    monkeypatch.setattr(stream, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(stream, "PING_INTERVAL", 0.0)
    monkeypatch.setattr(stream, "MAX_DURATION", 0.3)
    app, plugin = make_app()
    with _client(app) as c:
        since = time.time() - 1
        c.get("/json")
        plugin.store.flush(5)
        r = c.get(f"/_redline/api/stream?since={since}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        assert r.headers["x-accel-buffering"] == "no"
        assert r.headers["x-frame-options"] == "DENY"
        assert "event: trace\n" in r.text and '"path":"/json"' in r.text
        assert ": ping" in r.text
        paths = [t["path"] for t in plugin.store.list(None, limit=50).items]
        assert all(not p.startswith("/_redline") for p in paths)  # the stream is not recorded
        assert c.get("/_redline/api/stream?since=nope").json()["error"]["code"] == (
            "invalid_filter"
        )


def test_sse_stream_is_guarded(tmp_path):
    plugin = RedlinePlugin(settings={"storage_path": str(tmp_path / "g.db"), "access": "local"})
    app = Litestar(route_handlers=[page], plugins=[plugin])
    with TestClient(app, base_url="http://example.com") as c:
        assert c.get("/_redline/api/stream").status_code == 403
    plugin.close()


# ---------------------------------------------------------------------------- snapshot
def test_snapshot_passthrough(make_app):
    app, plugin = make_app()
    with _client(app) as c:
        c.get("/json")
        plugin.store.flush(5)
        tid = plugin.store.list(None, limit=1).items[0]["id"]
        r = c.get(f"/_redline/api/traces/{tid}/snapshot")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["content-disposition"] == f'attachment; filename="redline-{tid}.html"'
        assert r.headers["x-frame-options"] == "DENY"
        assert "__REDLINE_SNAPSHOT__" in r.text


# ---------------------------------------------------------------------------- runs
RUN_BODY = {
    "name": "smoke",
    "base_url": "http://evil.example",
    "requests": [{"method": "GET", "path": "/page"}],
    "profile": {"mode": "constant", "rps": 1, "duration_s": 1},
}


def test_runs_post_disabled(make_app):
    app, _ = make_app()
    with _client(app) as c:
        r = c.post("/_redline/api/runs", json=RUN_BODY)
        assert r.status_code == 403 and r.json()["error"]["code"] == "load_disabled"


def test_runs_post_launches_against_app_origin(make_app, monkeypatch):
    seen = {}

    def fake_launch(config, base_url, store_path, **kw):
        seen.update(config=config, base_url=base_url, store_path=store_path)
        return "run42"

    monkeypatch.setattr(control, "launch_background", fake_launch)
    app, plugin = make_app(settings={"load_from_ui": True})
    with _client(app) as c:
        r = c.post("/_redline/api/runs", json=RUN_BODY)
        assert r.status_code == 202, r.text
        assert r.json() == {"id": "run42", "name": "smoke", "status": "running"}
        assert seen["base_url"] == "http://127.0.0.1:80"  # scope["server"], never Host
        assert "base_url" not in seen["config"]
        assert seen["store_path"] == str(plugin.store.path)
        assert c.get("/_redline/api/runs/run42").json()["status"] == "running"


# ---------------------------------------------------------------------------- blocking
def test_blocking_call_in_async_handler_is_recorded(make_app):
    app, plugin = make_app(settings={"blocking_ms": 20})
    with _client(app) as c:
        c.get("/blocking")
        plugin.store.flush(5)
        items = plugin.store.list(None, limit=10).items
        trace = plugin.store.get(next(t for t in items if t["path"] == "/blocking")["id"])
    spans = [s for s in trace.spans if s.kind == "blocking"]
    assert spans and spans[0].duration_ms >= 20
    assert trace.framework == "litestar"


# ---------------------------------------------------------------------------- checks
CHECK_SUITE = {"repeat": 1, "warmup": 0, "requests": [{"name": "home", "path": "/"}]}


def test_checks_suites_and_runs(make_app, monkeypatch):
    from redline_core.check import control as check_control

    seen = {}

    def fake_launch(suite, base_url, store_path, **kw):
        seen.update(base_url=base_url, store_path=store_path, **kw)
        return kw["run_id"]

    monkeypatch.setattr(check_control, "launch_check", fake_launch)
    app, _plugin = make_app(debug=False, settings={"access": "local"})
    with _client(app) as c:
        r = c.post("/_redline/api/checks/suites", json={"name": "s", "suite": CHECK_SUITE})
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        r = c.put(f"/_redline/api/checks/suites/{sid}", json={"name": "s2", "suite": CHECK_SUITE})
        assert r.status_code == 200 and r.json()["name"] == "s2"
        r = c.post("/_redline/api/checks/runs", json={"suite_id": sid, "kind": "baseline"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "checks_from_ui_disabled"
    app, _plugin = make_app(debug=True)  # "auto": allowed in debug mode
    with _client(app) as c:
        sid = c.post("/_redline/api/checks/suites", json={"name": "s", "suite": CHECK_SUITE})
        sid = sid.json()["id"]
        r = c.post("/_redline/api/checks/runs", json={"suite_id": sid, "kind": "baseline"})
        assert r.status_code == 202, r.text
        assert seen["base_url"] == "http://127.0.0.1:80"  # scope["server"], never Host
        assert c.get(f"/_redline/api/checks/runs/{r.json()['id']}").json()["status"] == "running"
        big = {"format": "redline-check-baseline", "version": 1, "requests": [],
               "suite": {"name": "s", **CHECK_SUITE}, "created_at": 1767000000.0,
               "pad": "x" * (10 * 1000 * 1000 + 10)}  # fmt: skip
        r = c.post("/_redline/api/checks/baselines/import", json=big)  # > Litestar's 10 MB
        assert r.status_code == 201, r.text
