"""Badge helpers (``ui.badge_*``), the SSE stream (``redline_core.stream``) and the UI run
hooks (``load.control.run_hooks``)."""

from __future__ import annotations

import asyncio
import html
import json
import time

import pytest

from redline_core import Recorder, Settings, stream, ui
from redline_core.api import RedlineAPI
from redline_core.load import control
from redline_core.recorder import record_query
from redline_core.storage import FilterError, SQLiteStore


# ---------------------------------------------------------------------------- badge
def test_badge_snippet_escapes_and_carries_data():
    summary = {"duration_ms": 1.5, "query_count": 3, "query_ms": 0.4, "issue_count": None,
               "issue_kinds": [], "status": 200, "x": "'<&>\""}  # fmt: skip
    tag = ui.badge_snippet("abc", "/app/_redline/", summary)
    assert tag.startswith('<script src="/app/_redline/badge.js" defer')
    assert 'data-redline-trace="abc"' in tag
    assert 'data-redline-mount="/app/_redline"' in tag
    assert "'<&>" not in tag
    raw = tag.split("data-redline-summary='", 1)[1].split("'></script>", 1)[0]
    assert json.loads(html.unescape(raw)) == summary


def test_inject_badge_before_last_body_case_insensitive():
    body = b"<html><body><p>a</body></p></BODY >\n</html>"
    out = ui.inject_badge(body, "<x>")
    assert out == b"<html><body><p>a</body></p><x></BODY >\n</html>"
    assert ui.inject_badge(b"<p>fragment</p>", "<x>") is None


def test_badge_enabled():
    assert ui.badge_enabled(Settings(), debug=True)
    assert not ui.badge_enabled(Settings(), debug=False)
    assert ui.badge_enabled(Settings(badge=True), debug=False)
    assert not ui.badge_enabled(Settings(badge=False), debug=True)
    assert not ui.badge_enabled(Settings(badge=True, enabled=False), debug=True)


@pytest.mark.parametrize(
    ("csp", "ok"),
    [
        (None, True),
        ("frame-ancestors 'none'", True),
        ("default-src 'self'", True),
        ("script-src 'self' 'nonce-x'", True),
        ("script-src 'nonce-abc'", False),
        ("default-src 'none'", False),
        ("default-src 'none'; script-src *", True),
        ("script-src 'self', script-src 'none'", False),
    ],
)
def test_csp_allows_badge(csp, ok):
    assert ui.csp_allows_badge(csp) is ok


def test_badge_allowed():
    assert ui.badge_allowed(200, "text/html; charset=utf-8")
    assert not ui.badge_allowed(404, "text/html")
    assert not ui.badge_allowed(200, "application/json")
    assert not ui.badge_allowed(200, "text/html", content_encoding="gzip")
    assert not ui.badge_allowed(200, "text/html", csp="script-src 'none'")


def test_badge_summary_from_builder(tmp_path):
    rec = Recorder(Settings(), SQLiteStore(tmp_path / "r.db", background=False))
    b = rec.start_trace(method="GET", path="/")
    record_query("SELECT 1", start=time.perf_counter(), duration=0.002)
    s = ui.badge_summary(b, 200)
    assert s["query_count"] == 1 and s["status"] == 200
    assert s["issue_count"] is None and s["issue_kinds"] == []
    rec.finish_trace(b, status=200)


# ---------------------------------------------------------------------------- stream
def test_parse_since():
    assert stream.parse_since({"since": "12.5"}) == 12.5
    assert abs(stream.parse_since({}) - time.time()) < 5
    for bad in ("x", "nan", "-1"):
        with pytest.raises(FilterError):
            stream.parse_since({"since": bad})


class _FakeAPI:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    def poll(self, since, last_id=None, limit=100):
        self.calls.append((since, last_id))
        return self.batches.pop(0) if self.batches else []


def test_sync_events_trace_ping_and_close(monkeypatch):
    monkeypatch.setattr(stream, "POLL_INTERVAL", 0)
    monkeypatch.setattr(stream, "PING_INTERVAL", 2)
    monkeypatch.setattr(stream, "MAX_DURATION", 5)
    ticks = iter(range(100))
    api = _FakeAPI([[{"id": "t1", "started_at": 10.0}], []])
    frames = list(stream.sync_events(api, 1.0, sleep=lambda s: None, clock=lambda: next(ticks)))
    assert frames[0].startswith(b"retry:")
    assert frames[1].startswith(b"event: trace\nid: t1\ndata: {")
    assert stream.PING in frames
    assert api.calls[1] == (10.0, "t1")  # the cursor advanced past the sent trace


def test_async_events_disconnect(monkeypatch):
    monkeypatch.setattr(stream, "POLL_INTERVAL", 0)

    async def gone():
        return True

    async def collect():
        return [f async for f in stream.async_events(_FakeAPI([[{"id": "a", "started_at": 2}]]),
                                                     0.0, gone)]  # fmt: skip

    frames = asyncio.run(collect())
    assert len(frames) == 2 and b"event: trace" in frames[1]


def test_sync_events_with_real_store(tmp_path, monkeypatch):
    monkeypatch.setattr(stream, "POLL_INTERVAL", 0)
    monkeypatch.setattr(stream, "MAX_DURATION", 0)
    store = SQLiteStore(tmp_path / "r.db", background=False)
    rec = Recorder(Settings(), store)
    since = time.time() - 1
    rec.finish_trace(rec.start_trace(method="GET", path="/x"), status=200)
    frames = list(stream.sync_events(RedlineAPI(store), since))
    assert any(b'"path":"/x"' in f for f in frames)


# ---------------------------------------------------------------------------- run hooks
def test_run_hooks_need_load_from_ui_and_a_file_store(tmp_path):
    file_store = SQLiteStore(tmp_path / "r.db", background=False)
    assert control.run_hooks(file_store, Settings()) == (None, None)
    assert control.run_hooks(SQLiteStore(":memory:"), Settings(load_from_ui=True)) == (None, None)
    assert control.run_hooks(object(), Settings(load_from_ui=True)) == (None, None)
    launcher, stopper = control.run_hooks(file_store, Settings(load_from_ui=True))
    assert callable(launcher) and callable(stopper)


def test_run_hooks_launch_preinserts_running_row(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "r.db", background=False)
    seen = {}

    def fake_launch(config, base_url, store_path, **kw):
        seen.update(config=config, base_url=base_url, store_path=store_path)
        return "run1"

    monkeypatch.setattr(control, "launch_background", fake_launch)
    api_settings = Settings(load_from_ui=True)
    launcher, stopper = control.run_hooks(store, api_settings)
    api = RedlineAPI(store, api_settings, run_launcher=launcher, run_stopper=stopper)
    body = {"name": "r", "base_url": "http://evil.example", "workers": 999,
            "requests": [{"method": "GET", "path": "/"}],
            "profile": {"mode": "constant", "rps": 1, "duration_s": 1}}  # fmt: skip
    status, payload = api.dispatch("POST", "api/runs", body=body, base_url="http://127.0.0.1:8000")
    assert status == 202 and payload["id"] == "run1"
    assert seen["base_url"] == "http://127.0.0.1:8000"
    assert "base_url" not in seen["config"] and "workers" not in seen["config"]
    assert seen["store_path"] == str(tmp_path / "r.db")
    assert store.get_run("run1").status == "running"
    assert api.dispatch("POST", "api/runs/run1/stop") == (200, {"stopped": False})


def test_run_hooks_without_base_url_is_400(tmp_path):
    store = SQLiteStore(tmp_path / "r.db", background=False)
    s = Settings(load_from_ui=True)
    launcher, _ = control.run_hooks(store, s)
    api = RedlineAPI(store, s, run_launcher=launcher)
    body = {"name": "r", "requests": [{"path": "/"}],
            "profile": {"mode": "constant", "rps": 1, "duration_s": 1}}  # fmt: skip
    assert api.dispatch("POST", "api/runs", body=body)[0] == 400
