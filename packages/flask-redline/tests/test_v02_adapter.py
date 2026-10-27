"""v0.2 adapter features: Server-Timing, badge, SSE, snapshot, UI runs, async blocking, CLI."""

from __future__ import annotations

import html
import json

import pytest

from redline_core import stream
from redline_core.load import control

RUN_BODY = {
    "name": "smoke",
    "base_url": "http://evil.example",
    "requests": [{"method": "GET", "path": "/html"}],
    "profile": {"mode": "constant", "rps": 1, "duration_s": 1},
}


# ---------------------------------------------------------------------------- Server-Timing
def test_server_timing_header(make_env):
    env = make_env(settings={"server_timing": True})
    r = env.client().get("/orders/1")
    timing = r.headers["Server-Timing"]
    assert timing.startswith("total;dur=")
    assert "db;dur=" in timing and 'desc="1 query"' in timing
    trace_id = env.traces()[0]["id"]
    assert f'redline;desc="{trace_id}"' in timing


def test_server_timing_appends_to_the_apps_header(make_env):
    r = make_env(settings={"server_timing": True}).client().get("/timed")
    values = r.headers.getlist("Server-Timing")
    assert len(values) == 1
    assert values[0].startswith('app;dur=1.0;desc="mine", total;dur=')


def test_server_timing_off_and_not_on_redline_urls(make_env):
    env = make_env(settings={"server_timing": False})
    assert "Server-Timing" not in env.client().get("/orders/1").headers
    env2 = make_env(settings={"server_timing": True})
    assert "Server-Timing" not in env2.client().get("/_redline/api/meta").headers


def test_server_timing_auto_follows_app_debug(make_env):
    env = make_env()  # "auto": only in debug mode (it is a side channel)
    assert "Server-Timing" not in env.client().get("/orders/1").headers
    env.app.debug = True
    assert "Server-Timing" in env.client().get("/orders/1").headers


# ---------------------------------------------------------------------------- badge
def _badge(r):
    text = r.get_data(as_text=True)
    return text if "data-redline-trace" in text else None


def test_badge_injected_in_debug_html(make_env):
    env = make_env()
    env.app.debug = True
    r = env.client().get("/html")
    text = _badge(r)
    assert text is not None
    assert text.index("badge.js") < text.index("</BODY>")
    assert int(r.headers["Content-Length"]) == len(r.data)
    assert 'src="/_redline/badge.js"' in text
    trace_id = env.traces()[0]["id"]
    assert f'data-redline-trace="{trace_id}"' in text
    raw = text.split("data-redline-summary='", 1)[1].split("'", 1)[0]
    summary = json.loads(html.unescape(raw))
    assert summary["status"] == 200 and summary["issue_count"] is None
    # the recorded response body is the injected one, and the size matches
    assert env.trace("/html").response["size"] == len(r.data)


def test_badge_auto_follows_debug_and_setting_overrides(make_env):
    env = make_env()
    assert _badge(env.client().get("/html")) is None  # debug off
    forced = make_env(settings={"badge": True})
    assert _badge(forced.client().get("/html")) is not None
    off = make_env(settings={"badge": False})
    off.app.debug = True
    assert _badge(off.client().get("/html")) is None


@pytest.mark.parametrize(
    "path",
    [
        "/orders/1",
        "/html-csp",
        "/html-csp-two",
        "/html-download",
        "/html-fragment",
        "/file",
        "/missing",
        "/_redline/",
    ],
)
def test_badge_not_injected(make_env, path):
    env = make_env(settings={"badge": True})
    r = env.client().get(path)
    assert _badge(r) is None
    if r.headers.get("Content-Length"):
        assert int(r.headers["Content-Length"]) == len(r.data)


def test_badge_not_injected_into_streams(make_env):
    env = make_env(settings={"badge": True})
    r = env.client().get("/html-stream", buffered=False)
    assert b"".join(r.response) == b"<html><body>streamed</body></html>"
    r.close()


def test_badge_under_script_root(make_env):
    env = make_env(settings={"badge": True})
    r = env.client().get("/html", environ_overrides={"SCRIPT_NAME": "/app"})
    assert 'src="/app/_redline/badge.js"' in _badge(r)


def test_head_request_with_badge_keeps_length(make_env):
    env = make_env(settings={"badge": True})
    r = env.client().head("/html")
    assert r.status_code == 200 and r.data == b""


def test_top_level_static_files_are_served(make_env, monkeypatch, tmp_path):
    from redline_core import ui

    js = tmp_path / "badge.js"
    js.write_text("/* badge */")
    monkeypatch.setattr(ui, "asset_path", lambda name: js if name == "badge.js" else None)
    r = make_env().client().get("/_redline/badge.js")
    assert r.status_code == 200 and r.data == b"/* badge */"
    assert r.headers["Content-Type"].startswith("text/javascript")
    assert r.headers["X-Frame-Options"] == "DENY"


# ---------------------------------------------------------------------------- SSE
@pytest.fixture
def fast_stream(monkeypatch):
    monkeypatch.setattr(stream, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(stream, "PING_INTERVAL", 0.0)
    monkeypatch.setattr(stream, "MAX_DURATION", 0.3)


def test_sse_streams_new_traces_and_pings(make_env, fast_stream):
    env = make_env()
    c = env.client()
    c.get("/orders/1")
    env.store.flush(5)
    r = c.get("/_redline/api/stream?since=0", buffered=False)
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/event-stream")
    assert r.headers["Cache-Control"] == "no-cache"
    assert r.headers["X-Accel-Buffering"] == "no"
    assert r.headers["X-Frame-Options"] == "DENY"
    body = b"".join(r.response).decode()
    r.close()
    assert "event: trace" in body and '"path":"/orders/1"' in body
    assert ": ping" in body
    # the stream itself is never recorded
    assert [t["path"] for t in env.traces()] == ["/orders/1"]


def test_sse_bad_since_and_guard(make_env, fast_stream):
    env = make_env()
    r = env.client().get("/_redline/api/stream?since=nope")
    assert r.status_code == 400 and r.json["error"]["code"] == "invalid_filter"
    remote = env.client().get(
        "/_redline/api/stream", environ_overrides={"REMOTE_ADDR": "203.0.113.9"}
    )
    assert remote.status_code == 403


# ---------------------------------------------------------------------------- snapshot
def test_snapshot_is_sent_raw(make_env):
    env = make_env()
    c = env.client()
    c.get("/orders/1")
    env.store.flush(5)
    trace_id = env.traces()[0]["id"]
    r = c.get(f"/_redline/api/traces/{trace_id}/snapshot")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "text/html; charset=utf-8"
    assert r.headers["Content-Disposition"] == f'attachment; filename="redline-{trace_id}.html"'
    assert r.headers["X-Frame-Options"] == "DENY"
    assert b"__REDLINE_SNAPSHOT__" in r.data
    missing = c.get("/_redline/api/traces/nope/snapshot")
    assert missing.status_code == 404 and missing.json["error"]["code"] == "trace_not_found"


# ---------------------------------------------------------------------------- runs
def test_runs_post_refused_when_load_from_ui_off(make_env):
    r = make_env().client().post("/_redline/api/runs", json=RUN_BODY)
    assert r.status_code == 403 and r.json["error"]["code"] == "load_disabled"


def test_runs_post_launches_against_the_app_origin(make_env, monkeypatch):
    seen = {}

    def fake_launch(config, base_url, store_path, **kw):
        seen.update(config=config, base_url=base_url, store_path=store_path)
        return "run123"

    monkeypatch.setattr(control, "launch_background", fake_launch)
    env = make_env(settings={"load_from_ui": True})
    c = env.client()
    r = c.post("/_redline/api/runs", json=RUN_BODY)
    assert r.status_code == 202, r.json
    assert r.json == {"id": "run123", "name": "smoke", "status": "running"}
    assert seen["base_url"] == "http://127.0.0.1:80"  # SERVER_PORT, never the Host header
    assert "base_url" not in seen["config"]
    assert seen["store_path"] == env.redline.store.path
    assert c.get("/_redline/api/runs/run123").json["status"] == "running"
    sub = c.post("/_redline/api/runs", json=RUN_BODY, environ_overrides={"SCRIPT_NAME": "/app"})
    assert sub.status_code == 202 and seen["base_url"] == "http://127.0.0.1:80/app"
    cross = c.post("/_redline/api/runs", json=RUN_BODY, headers={"Origin": "http://evil.example"})
    assert cross.status_code == 403 and cross.json["error"]["code"] == "csrf_failed"


def test_runs_stop_uses_the_stop_file(make_env, monkeypatch):
    monkeypatch.setattr(control, "launch_background", lambda *a, **k: "run9")
    calls = []
    monkeypatch.setattr(control, "stop_run", lambda run_id, path: calls.append(run_id) or True)
    env = make_env(settings={"load_from_ui": True})
    c = env.client()
    assert c.post("/_redline/api/runs", json=RUN_BODY).status_code == 202
    assert c.post("/_redline/api/runs/run9/stop").json == {"stopped": True}
    assert calls == ["run9"]


# ---------------------------------------------------------------------------- async
def test_async_view_blocking_is_detected(make_env):
    env = make_env()
    r = env.client().get("/async-sleep")
    assert r.status_code == 200
    t = env.trace("/async-sleep")
    blocking = [s for s in t.spans if s.kind == "blocking"]
    assert blocking, [s.kind for s in t.spans]
    assert sum(s.duration_ms for s in blocking) >= 40
    assert any(i.kind == "blocking_in_async" for i in t.issues)


def test_sync_view_has_no_blocking_span(make_env):
    env = make_env()
    env.client().get("/slow/1")
    assert not [s for s in env.trace("/slow/1").spans if s.kind == "blocking"]


# ---------------------------------------------------------------------------- CLI
def run(env, *args, **kw):
    return env.app.test_cli_runner().invoke(args=["redline", *args], **kw)


def test_cli_scan_text_json_and_fail(make_env):
    env = make_env()
    r = run(env, "scan")
    assert r.exit_code == 0, r.output
    assert "customers.html:3" in r.output and "o.customer.name" in r.output
    j = run(env, "scan", "--json")
    findings = json.loads(j.output)
    assert any(f["loop_var"] == "o" and f["file"].endswith("customers.html") for f in findings)
    assert run(env, "scan", "--fail").exit_code == 1


def test_cli_scenario_from_url_map_and_traffic(make_env, tmp_path):
    env = make_env()
    r = run(env, "scenario")
    assert r.exit_code == 0, r.output
    assert "[[requests]]" in r.output
    assert 'path = "/html"' in r.output
    assert "/orders/" not in r.output  # converters, no traffic yet
    assert "_redline" not in r.output and "/static" not in r.output

    env.client().get("/orders/3")
    out = tmp_path / "scenario.json"
    r = run(env, "scenario", "-o", str(out), "--base-url", "http://127.0.0.1:8080")
    assert r.exit_code == 0, r.output
    data = json.loads(out.read_text())
    paths = {q["path"]: q["weight"] for q in data["requests"]}
    assert paths["/orders/3"] == 1 and paths["/html"] == 1
    assert data["base_url"] == "http://127.0.0.1:8080"
    assert run(env, "scenario", "-o", str(out)).exit_code != 0  # exists, no --force
    assert run(env, "scenario", "-o", str(out), "--force").exit_code == 0


# ---------------------------------------------------------------------------- checks
CHECK_SUITE = {"repeat": 1, "warmup": 0, "requests": [{"name": "home", "path": "/"}]}


def test_checks_suites_and_runs(make_env, monkeypatch):
    from redline_core.check import control as check_control

    seen = {}

    def fake_launch(suite, base_url, store_path, **kw):
        seen.update(base_url=base_url, store_path=store_path, **kw)
        return kw["run_id"]

    monkeypatch.setattr(check_control, "launch_check", fake_launch)
    env = make_env()
    c = env.client()
    r = c.post("/_redline/api/checks/suites", json={"name": "s", "suite": CHECK_SUITE})
    assert r.status_code == 201, r.json
    sid = r.json["id"]
    r = c.put(f"/_redline/api/checks/suites/{sid}", json={"name": "s2", "suite": CHECK_SUITE})
    assert r.status_code == 200 and r.json["name"] == "s2"
    r = c.post("/_redline/api/checks/runs", json={"suite_id": sid, "kind": "baseline"})
    assert r.status_code == 403 and r.json["error"]["code"] == "checks_from_ui_disabled"
    env.app.debug = True  # "auto" follows app.debug, read per request
    r = c.post("/_redline/api/checks/runs", json={"suite_id": sid, "kind": "baseline"})
    assert r.status_code == 202, r.json
    assert seen["base_url"] == "http://127.0.0.1:80"  # SERVER_PORT, never the Host header
    assert c.get(f"/_redline/api/checks/runs/{r.json['id']}").json["status"] == "running"
    r = c.get(f"/_redline/api/checks/suites/{sid}/export")
    assert r.status_code == 200 and r.mimetype == "text/plain"
