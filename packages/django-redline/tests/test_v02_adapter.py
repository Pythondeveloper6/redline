"""v0.2 adapter surface: Server-Timing, badge, SSE, snapshot, runs, redline_scenario."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from redline_core import stream
from redline_core.load import control

pytestmark = pytest.mark.django_db

BASE = {"STORAGE_PATH": ":memory:", "SAMPLE_RATE": 1.0}


def _redline(**extra):
    return override_settings(REDLINE={**BASE, **extra})


# ---------------------------------------------------------------------------- Server-Timing
@_redline(SERVER_TIMING=True)
def test_server_timing_on_recorded_response(client, shop):
    response = client.get("/n1/reverse-fk/")
    value = response["Server-Timing"]
    assert value.startswith("total;dur=")
    assert "db;dur=" in value and 'redline;desc="' in value


@_redline(SERVER_TIMING=True)
def test_server_timing_appends_to_existing(client):
    value = client.get("/html/?timing=1")["Server-Timing"]
    assert value.startswith("app;dur=1, total;dur=")


@_redline(SERVER_TIMING=False)
def test_server_timing_off(client):
    assert "Server-Timing" not in client.get("/plain/")


def test_server_timing_auto_follows_debug(client, settings):
    settings.DEBUG = False  # "auto" (the default): a side channel, so only in DEBUG
    assert "Server-Timing" not in client.get("/plain/")
    settings.DEBUG = True
    assert "Server-Timing" in client.get("/plain/")


def test_no_server_timing_on_redline_urls(staff_client):
    assert "Server-Timing" not in staff_client.get("/redline/api/meta")


# ---------------------------------------------------------------------------- badge
def _badge(response) -> bool:
    return b'data-redline-trace="' in response.content


@override_settings(DEBUG=True)
def test_badge_auto_in_debug(client):
    response = client.get("/html/?length=1")
    body = response.content
    assert _badge(response)
    assert b'<script src="/redline/badge.js" defer' in body
    assert b'data-redline-mount="/redline"' in body
    assert body.index(b"data-redline-trace") < body.index(b"</body>")
    assert response["Content-Length"] == str(len(body))
    assert response["X-Redline-Trace"].encode() in body


def test_badge_auto_off_without_debug(client):
    assert not _badge(client.get("/html/"))


@_redline(BADGE=True)
def test_badge_forced_on(client):
    assert _badge(client.get("/html/"))


@override_settings(DEBUG=True)
@_redline(BADGE=False)
def test_badge_forced_off(client):
    assert not _badge(client.get("/html/"))


@override_settings(DEBUG=True)
@pytest.mark.parametrize(
    "url",
    [
        "/html-stream/",
        "/file/",
        "/plain/",
        "/missing/",
        "/html/?csp=script-src%20'none'",
        "/html/?csp=script-src%20'self'%20'strict-dynamic'",
        "/html/?download=1",
    ],
)
def test_badge_not_injected(client, url):
    response = client.get(url)
    content = b"".join(response.streaming_content) if response.streaming else response.content
    assert b"data-redline-trace" not in content


@override_settings(DEBUG=True)
def test_badge_with_csp_allowing_self(client):
    assert _badge(client.get("/html/?csp=script-src%20'self'"))


@override_settings(DEBUG=True)
def test_no_badge_on_redline_pages(staff_client):
    assert b"data-redline-trace" not in staff_client.get("/redline/").content


def test_top_level_bundle_file(client, tmp_path, monkeypatch):
    from redline_core import ui

    js = tmp_path / "badge.js"
    js.write_text("/* badge */")
    monkeypatch.setattr(ui, "asset_path", lambda name: js if name == "badge.js" else None)
    response = client.get("/redline/badge.js")  # no login needed: the file holds no data
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/javascript")
    assert b"".join(response.streaming_content) == b"/* badge */"
    assert response["X-Frame-Options"] == "DENY"
    assert client.get("/redline/nope.js").status_code == 404
    assert client.get("/redline/index.html").status_code == 404


# ---------------------------------------------------------------------------- SSE
@pytest.fixture
def fast_stream(monkeypatch):
    monkeypatch.setattr(stream, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(stream, "PING_INTERVAL", 0.0)
    monkeypatch.setattr(stream, "MAX_DURATION", 0.05)


def test_stream_requires_access(client):
    response = client.get("/redline/api/stream")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_stream_sends_new_traces_and_pings(staff_client, client, latest, fast_stream):
    client.get("/plain/")
    trace = latest()
    response = staff_client.get(f"/redline/api/stream?since={trace.started_at - 1}")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/event-stream")
    assert response["Cache-Control"] == "no-cache"
    assert response["X-Accel-Buffering"] == "no"
    body = b"".join(response.streaming_content).decode()
    assert "event: trace\n" in body and f'"id":"{trace.id}"' in body
    assert ": ping" in body
    assert latest().id == trace.id  # the stream itself was not recorded


def test_stream_invalid_since(staff_client):
    response = staff_client.get("/redline/api/stream?since=abc")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_filter"


async def test_stream_async_generator_under_asgi(async_client, latest, fast_stream):
    from asgiref.sync import sync_to_async
    from django.contrib.auth import get_user_model

    user = await sync_to_async(get_user_model().objects.create_user)(
        "astaff", password="pw", is_staff=True, is_superuser=True
    )
    await async_client.aforce_login(user)
    response = await async_client.get("/redline/api/stream?since=0")
    assert response.status_code == 200
    assert response.is_async
    chunks = [c async for c in response.streaming_content]
    assert chunks[0].startswith(b"retry:")


# ---------------------------------------------------------------------------- snapshot
def test_snapshot_passthrough(staff_client, client, latest):
    client.get("/plain/")
    trace = latest()
    response = staff_client.get(f"/redline/api/traces/{trace.id}/snapshot")
    assert response.status_code == 200
    assert response["Content-Type"] == "text/html; charset=utf-8"
    assert response["Content-Disposition"] == f'attachment; filename="redline-{trace.id}.html"'
    assert response["X-Frame-Options"] == "DENY"
    assert b"__REDLINE_SNAPSHOT__" in response.content
    assert staff_client.get("/redline/api/traces/nope/snapshot").status_code == 404


def test_json_api_denies_framing(staff_client):
    assert staff_client.get("/redline/api/meta")["X-Frame-Options"] == "DENY"


# ---------------------------------------------------------------------------- runs
RUN_BODY = {
    "name": "ui",
    "base_url": "http://evil.example",
    "requests": [{"method": "GET", "path": "/plain/"}],
    "profile": {"mode": "constant", "rps": 1, "duration_s": 1},
}


def _post_run(client, staff):
    client.force_login(staff)
    client.get("/redline/")
    token = client.cookies["csrftoken"].value
    return client.post(
        "/redline/api/runs",
        data=json.dumps(RUN_BODY),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )


def test_runs_post_disabled(client, staff):
    response = _post_run(client, staff)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "load_disabled"


def test_runs_post_needs_csrf(staff_client):
    response = staff_client.post(
        "/redline/api/runs", data=json.dumps(RUN_BODY), content_type="application/json"
    )
    assert response.status_code in (403,)


def test_runs_post_launches_against_own_origin(client, staff, tmp_path, monkeypatch):
    seen = {}

    def fake_launch(config, base_url, store_path, **kw):
        seen.update(config=config, base_url=base_url, store_path=store_path)
        return "run42"

    monkeypatch.setattr(control, "launch_background", fake_launch)
    db = tmp_path / "r.db"
    with _redline(STORAGE_PATH=str(db), LOAD_FROM_UI=True, ALLOWED_HOSTS=["testserver"]):
        response = _post_run(client, staff)
        assert response.status_code == 202, response.content
        assert response.json() == {"id": "run42", "name": "ui", "status": "running"}
        # the server's own listening port (SERVER_PORT), never the Host header (M5)
        assert seen["base_url"] == "http://127.0.0.1:80"
        assert "base_url" not in seen["config"]
        assert seen["store_path"] == str(db)
        run = client.get("/redline/api/runs/run42").json()
        assert run["status"] == "running"


@_redline(LOAD_FROM_UI=True)
def test_runs_post_memory_store_not_supported(client, staff):
    assert _post_run(client, staff).status_code == 501


# ---------------------------------------------------------------------------- scenario
def test_redline_scenario_stdout(client, shop, latest):
    client.get("/api/orders/1")  # fills the Ninja "/api/orders/<int:order_id>" template
    client.get("/plain/3/")
    latest()
    out = StringIO()
    call_command("redline_scenario", "-o", "-", stdout=out)
    text = out.getvalue()
    assert "[[requests]]" in text
    assert 'path = "/plain/"' in text
    assert 'path = "/plain/3/"' in text
    assert 'path = "/api/orders/1"' in text
    assert 'path = "/api/orders"' in text
    assert "/redline" not in text and "/admin" not in text
    assert "touch" not in text  # a POST-only Ninja route
    assert 'name = "GET /drf/orders/"' in text  # DRF APIView with get()


def test_redline_scenario_file(tmp_path):
    target = tmp_path / "s.json"
    out = StringIO()
    call_command("redline_scenario", "-o", str(target), stdout=out)
    data = json.loads(target.read_text())
    assert data["base_url"] == "http://127.0.0.1:8000"
    assert all(r["weight"] == 1 and "<" not in r["path"] for r in data["requests"])
    with pytest.raises(CommandError):
        call_command("redline_scenario", "-o", str(target), stdout=StringIO())
    call_command("redline_scenario", "-o", str(target), "--force", stdout=StringIO())


def test_iter_routes_regex_patterns():
    from django.urls import re_path

    from django_redline.management.commands.redline_scenario import iter_routes

    def view(request):  # pragma: no cover
        return None

    routes = list(iter_routes([re_path(r"^plain/$", view), re_path(r"^item/(?P<pk>\d+)/$", view)]))
    assert routes == [("GET", "/plain/$", "/plain/"), ("GET", "/item/(?P<pk>\\d+)/$", None)]


# ---------------------------------------------------------------------------- checks
CHECK_SUITE = {"repeat": 1, "warmup": 0, "requests": [{"name": "home", "path": "/plain/"}]}


def _send(client, method, path, body):
    client.get("/redline/")
    token = client.cookies["csrftoken"].value
    return getattr(client, method)(
        path, data=json.dumps(body), content_type="application/json", HTTP_X_CSRFTOKEN=token
    )


def test_checks_suites_and_runs(client, staff, tmp_path, monkeypatch):
    from redline_core.check import control as check_control

    seen = {}

    def fake_launch(suite, base_url, store_path, **kw):
        seen.update(base_url=base_url, store_path=store_path, **kw)
        return kw["run_id"]

    monkeypatch.setattr(check_control, "launch_check", fake_launch)
    client.force_login(staff)
    with _redline(STORAGE_PATH=str(tmp_path / "r.db"), ALLOWED_HOSTS=["testserver"]):
        r = _send(client, "post", "/redline/api/checks/suites", {"name": "s", "suite": CHECK_SUITE})
        assert r.status_code == 201, r.content
        sid = r.json()["id"]
        r = _send(
            client, "put", f"/redline/api/checks/suites/{sid}", {"name": "s2", "suite": CHECK_SUITE}
        )
        assert r.status_code == 200 and r.json()["name"] == "s2"
        body = {"suite_id": sid, "kind": "baseline"}
        r = _send(client, "post", "/redline/api/checks/runs", body)  # DEBUG off: "auto" refuses
        assert r.status_code == 403 and r.json()["error"]["code"] == "checks_from_ui_disabled"
        with override_settings(DEBUG=True):
            r = _send(client, "post", "/redline/api/checks/runs", body)
        assert r.status_code == 202, r.content
        assert seen["base_url"] == "http://127.0.0.1:80"
        run = client.get(f"/redline/api/checks/runs/{r.json()['id']}").json()
        assert run["status"] == "running"
        # bodies over DATA_UPLOAD_MAX_MEMORY_SIZE still reach the import endpoint
        big = {"format": "redline-check-baseline", "version": 1, "requests": [],
               "suite": {"name": "s", **CHECK_SUITE}, "created_at": 1767000000.0,
               "pad": "x" * (3 * 1024 * 1024)}  # fmt: skip
        r = _send(client, "post", "/redline/api/checks/baselines/import", big)
        assert r.status_code == 201, r.content


def test_runtime_applies_auto_instrument_setting(monkeypatch, tmp_path):
    from django_redline.runtime import build_runtime
    from redline_core import Settings

    seen = []
    monkeypatch.setattr(
        "django_redline.runtime.auto_instrument", lambda s: seen.append(s.auto_instrument)
    )
    runtime = build_runtime(Settings(storage_path=str(tmp_path / "r.db")))
    try:
        assert seen == ["warn"]
    finally:
        runtime.close() if hasattr(runtime, "close") else None
