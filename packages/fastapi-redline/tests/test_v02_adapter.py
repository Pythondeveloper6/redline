"""v0.2 adapter features: Server-Timing, badge injection, SSE, snapshot, runs from the UI."""

from __future__ import annotations

import html
import json
import time

import pytest
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from redline_core import stream
from redline_core.load import control

PAGE = "<html><head></head><body><h1>Hi</h1></body></html>"


def _add_pages(app, tmp_path):
    @app.get("/page")
    async def page():
        return HTMLResponse(PAGE)

    @app.get("/page-csp")
    async def page_csp():
        return HTMLResponse(PAGE, headers={"Content-Security-Policy": "script-src 'nonce-x'"})

    @app.get("/page-timing")
    async def page_timing():
        return HTMLResponse(PAGE, headers={"Server-Timing": 'app;desc="mine"'})

    @app.get("/page-404")
    async def page_404():
        return HTMLResponse(PAGE, status_code=404)

    @app.get("/page-stream")
    async def page_stream():
        async def gen():
            yield b"<html><body>"
            yield b"</body></html>"

        return StreamingResponse(gen(), media_type="text/html")

    @app.get("/page-json")
    async def page_json():
        return JSONResponse({"html": "</body>"})

    file = tmp_path / "f.html"
    file.write_text(PAGE)

    @app.get("/page-file")
    async def page_file():
        return FileResponse(file, media_type="text/html")


def _badge(text: str) -> dict | None:
    if "data-redline-summary='" not in text:
        return None
    raw = text.split("data-redline-summary='", 1)[1].split("'", 1)[0]
    return json.loads(html.unescape(raw))


def test_server_timing_header(make_env, tmp_path):
    env = make_env(settings={"server_timing": True})
    _add_pages(env.app, tmp_path)
    with TestClient(env.app) as c:
        r = c.get("/orders/1")
        st = r.headers["server-timing"]
        assert st.startswith("total;dur=") and "db;dur=" in st and 'redline;desc="' in st
        r = c.get("/page-timing")
        assert r.headers["server-timing"].startswith('app;desc="mine", total;dur=')
        assert "server-timing" not in c.get("/_redline/api/meta").headers


def test_server_timing_off(make_env):
    env = make_env(settings={"server_timing": False})
    with TestClient(env.app) as c:
        assert "server-timing" not in c.get("/orders/1").headers


def test_server_timing_auto_follows_app_debug(make_env):
    env = make_env()  # "auto": only in debug mode (it is a side channel)
    with TestClient(env.app) as c:
        assert "server-timing" not in c.get("/orders/1").headers
        env.app.debug = True
        assert "server-timing" in c.get("/orders/1").headers


def test_badge_injected_in_html(make_env, tmp_path):
    env = make_env(settings={"badge": True})
    _add_pages(env.app, tmp_path)
    with TestClient(env.app) as c:
        r = c.get("/page")
        assert r.text.endswith("</script></body></html>")
        assert int(r.headers["content-length"]) == len(r.content)
        assert 'src="/_redline/badge.js"' in r.text
        summary = _badge(r.text)
        assert summary["status"] == 200 and summary["issue_count"] is None
        trace_id = r.text.split('data-redline-trace="', 1)[1].split('"', 1)[0]
        env.redline.store.flush(5)
        assert c.get(f"/_redline/api/traces/{trace_id}").status_code == 200
        # the recorded response body is the app's, without the badge
        assert "badge.js" not in (env.trace("/page").response["body"] or "")


@pytest.mark.parametrize(
    "path", ["/page-csp", "/page-404", "/page-stream", "/page-json", "/page-file", "/_redline/"]
)
def test_badge_not_injected(make_env, tmp_path, path):
    env = make_env(settings={"badge": True})
    _add_pages(env.app, tmp_path)
    with TestClient(env.app) as c:
        r = c.get(path)
        assert "badge.js" not in r.text


def test_badge_auto_follows_app_debug(make_env, tmp_path):
    env = make_env()
    _add_pages(env.app, tmp_path)
    with TestClient(env.app) as c:
        assert "badge.js" not in c.get("/page").text
        env.app.debug = True
        assert "badge.js" in c.get("/page").text


def test_root_path_mount_url(make_env, tmp_path):
    env = make_env(settings={"badge": True})
    _add_pages(env.app, tmp_path)
    with TestClient(env.app, root_path="/sub") as c:
        assert 'data-redline-mount="/sub/_redline"' in c.get("/page").text


def test_sse_stream_sends_trace_and_ping(make_env, monkeypatch):
    monkeypatch.setattr(stream, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(stream, "PING_INTERVAL", 0.0)
    monkeypatch.setattr(stream, "MAX_DURATION", 0.3)
    env = make_env()
    with TestClient(env.app) as c:
        since = time.time() - 1
        c.get("/orders/1")
        env.redline.store.flush(5)
        r = c.get(f"/_redline/api/stream?since={since}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        assert r.headers["x-accel-buffering"] == "no"
        assert "event: trace" in r.text and '"path":"/orders/1"' in r.text
        assert ": ping" in r.text
        assert c.get("/_redline/api/stream?since=nope").status_code == 400
        env.redline.store.flush(5)
        assert all(t["path"] != "/_redline/api/stream" for t in env.traces())


def test_sse_stream_is_guarded(make_env):
    env = make_env(dependencies=None)
    with TestClient(env.app, base_url="http://evil.example") as c:
        assert c.get("/_redline/api/stream").status_code == 403


def test_snapshot_download(make_env):
    env = make_env()
    with TestClient(env.app) as c:
        c.get("/orders/1")
        env.redline.store.flush(5)
        tid = env.traces()[0]["id"]
        r = c.get(f"/_redline/api/traces/{tid}/snapshot")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["content-disposition"] == f'attachment; filename="redline-{tid}.html"'
        assert r.headers["x-frame-options"] == "DENY"
        assert "__REDLINE_SNAPSHOT__" in r.text


RUN = {"name": "r", "requests": [{"method": "GET", "path": "/orders/1"}],
       "profile": {"mode": "constant", "rps": 1, "duration_s": 1}}  # fmt: skip


def test_runs_post_disabled(make_env):
    env = make_env()
    with TestClient(env.app) as c:
        r = c.post("/_redline/api/runs", json=RUN)
        assert r.status_code == 403 and r.json()["error"]["code"] == "load_disabled"


def test_runs_post_launches_against_app_origin(make_env, monkeypatch):
    seen = {}

    def fake(config, base_url, store_path, **kw):
        seen.update(config=config, base_url=base_url, store_path=store_path)
        return "run42"

    monkeypatch.setattr(control, "launch_background", fake)
    env = make_env(settings={"load_from_ui": True})
    with TestClient(env.app, base_url="http://localhost:8000", root_path="/app") as c:
        r = c.post("/_redline/api/runs", json={**RUN, "base_url": "http://evil.example"})
        assert r.status_code == 202, r.text
        assert r.json()["id"] == "run42"
        assert seen["base_url"] == "http://localhost:8000/app"
        assert seen["store_path"] == env.redline.store.path
        assert c.get("/_redline/api/runs/run42").json()["status"] == "running"
        # cross-site POST is refused by the CSRF guard
        bad = c.post("/_redline/api/runs", json=RUN, headers={"Origin": "http://evil.example"})
        assert bad.status_code == 403
