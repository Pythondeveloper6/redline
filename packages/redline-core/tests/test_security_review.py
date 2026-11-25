"""Regressions for the branch's security review (each failed before its fix)."""

from __future__ import annotations

import base64
import json
import struct
import time
import zlib

import pytest

from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.histogram import Histogram
from redline_core.recorder import Recorder
from redline_core.runs import Run
from redline_core.storage import SQLiteStore
from redline_core.storage.postgres import mask_url


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(str(tmp_path / "s.db"), background=False)
    yield s
    s.close()


# --------------------------------------------------------------------- histogram bomb
def test_a_zip_bomb_histogram_is_refused_quickly():
    payload = b"\x01" + b"\x00" * 200_000_000  # inflates to ~200 MB of zero deltas
    bomb = b"RLH" + bytes((1,)) + zlib.compress(payload, 9)
    assert len(bomb) < 300_000
    t0 = time.monotonic()
    with pytest.raises(ValueError):
        Histogram.from_bytes(bomb)
    assert time.monotonic() - t0 < 2.0


def test_out_of_order_buckets_and_normal_histograms():
    h = Histogram()
    for v in (1.0, 5.0, 250.0, 9000.0):
        h.record(v)
    assert Histogram.from_bytes(h.to_bytes()) == h
    raw = bytearray()
    for n in (3, 5, 1, 0, 1, 0, 1, 1):  # bucket 5, then delta 0 twice: not increasing
        raw.append(n)
    with pytest.raises(ValueError):
        Histogram.from_bytes(
            b"RLH" + bytes((1,)) + zlib.compress(bytes(raw) + struct.pack("<ddd", 1, 1, 1))
        )


# --------------------------------------------------------------------- url masking
@pytest.mark.parametrize(
    "url",
    [
        "mssql+pyodbc:///?odbc_connect=DRIVER%3Dx%3BUID%3Dsa%3BPWD%3Dhunter2",
        "mssql+pyodbc://svc@host/db?driver=x&PWD=hunter2",
        "mssql+pyodbc://svc@host/db?password=hunter2&uid=sa",
        "postgresql://u:hunter2@host/db",
    ],
)
def test_urls_never_show_their_secret(url):
    assert "hunter2" not in mask_url(url)


# --------------------------------------------------------------------- run id abuse
def test_a_made_up_run_id_is_not_kept_and_does_not_hit_the_store(store, monkeypatch):
    rec = Recorder(Settings(sample_rate=0.0, always_keep_slower_than_ms=1e9), store)
    calls = []
    real = store.get_run
    monkeypatch.setattr(store, "get_run", lambda *a, **k: calls.append(a) or real(*a, **k))
    for i in range(30):
        with rec.recording(method="GET", path="/a", headers={"X-Redline-Run": f"fake{i};step=1"}):
            pass
    store.flush()
    assert store.count() == 0 and calls == []  # nothing queued, no run lookups


def test_a_real_run_is_still_kept(store):
    store.save_run(Run(id="real", name="x", status="running"))
    rec = Recorder(Settings(sample_rate=0.0, always_keep_slower_than_ms=1e9), store)
    with rec.recording(method="GET", path="/a", headers={"X-Redline-Run": "real;step=1"}):
        pass
    store.flush()
    assert store.count() == 1


# --------------------------------------------------------------------- remote registration caps
def _api(store):
    return RedlineAPI(store, Settings(accept_remote_runs=True))


def _hist():
    h = Histogram()
    h.record(5.0)
    return base64.b64encode(h.to_bytes()).decode()


def _step(n):
    return {
        "step": n, "target_rps": 1, "started_at": 1, "ended_at": 2, "achieved_rps": 1,
        "sent": 1, "ok": 1, "errors": 0, "dropped": 0, "hist": _hist(), "error_breakdown": {},
    }  # fmt: skip


def test_step_numbers_and_running_runs_are_capped(store):
    api = _api(store)
    body = {"id": "r1", "name": "x", "created_at": 1.0, "config": {}}
    assert api.dispatch("POST", "api/runs/register", body=body)[0] == 201
    assert api.dispatch("POST", "api/runs/r1/steps", body=_step(1000))[0] == 200
    assert api.dispatch("POST", "api/runs/r1/steps", body=_step(10**9))[0] == 400
    for i in range(2, 51):
        b = {**body, "id": f"r{i}"}
        assert api.dispatch("POST", "api/runs/register", body=b)[0] == 201
    assert api.dispatch("POST", "api/runs/register", body={**body, "id": "r51"})[0] == 429


def test_metrics_cannot_be_added_to_a_finished_run(store):
    api = _api(store)
    api.dispatch(
        "POST", "api/runs/register", body={"id": "m1", "name": "x", "created_at": 1.0, "config": {}}
    )
    api.dispatch("POST", "api/runs/m1/finish", body={"status": "done"})
    status, _ = api.dispatch("POST", "api/runs/m1/metrics", body={"step": 1, "values": {"a": 1}})
    assert status == 409


def test_deeply_nested_json_is_a_400_not_a_crash(store):
    deep = "[" * 100_000 + "]" * 100_000
    assert _api(store).dispatch("POST", "api/runs/register", body=deep)[0] == 400


# --------------------------------------------------------------------- a remote run is not reaped
def test_http_registered_runs_are_not_marked_dead(tmp_path):
    db = tmp_path / "r.db"
    (tmp_path / "runs").mkdir()  # a UI run once launched here: pid files are now expected
    s = SQLiteStore(str(db), background=False)
    api = _api(s)
    assert (
        api.dispatch(
            "POST",
            "api/runs/register",
            body={"id": "rem", "name": "x", "created_at": time.time() - 60, "config": {}},
        )[0]
        == 201
    )
    s.save_run(Run(id="local", name="y", status="running", created_at=time.time() - 60))
    assert s.get_run("rem").status == "running"  # no pid file, but registered over HTTP
    assert s.get_run("local").status == "failed"  # a local run with no process is still reaped
    assert api.dispatch("POST", "api/runs/rem/steps", body=_step(1))[0] == 200
    s.close()


# --------------------------------------------------------------------- capabilities endpoint
def test_capabilities_alias_is_validated_and_the_feature_switch_applies(store):
    api = RedlineAPI(store, Settings(), explain_executor=lambda a, v: None)
    assert api.dispatch("GET", "api/database/capabilities", {"db": "x" * 200})[0] == 400
    assert api.dispatch("GET", "api/database/capabilities", {"db": "a b;c"})[0] == 400
    from redline_core.access import route_policy

    off = Settings.from_mapping({"features": {"explain": False}})
    assert route_policy("GET", "api/database/capabilities", off).status == 404


def test_the_capability_cache_is_bounded():
    from redline_core import database_capabilities as dc

    dc.clear_cache()
    for i in range(200):
        dc.capabilities_for(f"db{i}", lambda sql, params: [("u", 1, 1, 0, 1, 3)], "mssql")
    assert len(dc._CACHE) <= 64
    dc.clear_cache()


# --------------------------------------------------------------------- redaction and masking
def test_redact_config_masks_auth_urls_and_websocket_messages():
    from redline_core.load.sinks import redact_config

    messages = ["token=abc123secretvalue&x=1", {"password": "pw-secret"}]
    token_request = {
        "method": "GET",
        "base_url": "https://u:pw-url@idp.example",
        "path": "/t?access_token=tok-secret-1",
    }
    cfg = {
        "base_url": "http://x",
        "requests": [{"path": "/ws", "type": "websocket", "messages": messages}],
        "auth": {"provider": "request", "request": token_request},
    }
    text = json.dumps(redact_config(cfg))
    for secret in ("pw-secret", "pw-url", "tok-secret-1"):
        assert secret not in text


def test_the_http_sink_error_hides_url_credentials():
    from redline_core.load.sinks import HttpSink, RegisterError

    sink = HttpSink("http://user:pw-9z@127.0.0.1:1/_redline", "t" * 10)
    with pytest.raises(RegisterError) as info:
        sink.preflight()
    assert "pw-9z" not in str(info.value)


# --------------------------------------------------------------------- MCP
def test_mcp_start_load_run_refuses_anything_that_can_run_code(tmp_path, monkeypatch):
    from redline_core.agent import tools_runs

    for key, _value in (
        ("auth", {"provider": "command", "command": ["sh", "-c", "id"]}),
        ("identities", [{"name": "u", "headers": {"X": "${HOME}"}}]),
        ("flows", [{"steps": [{"path": "/"}]}]),
        ("identity_mode", "random"),
        ("metric_sources", ["os:getcwd"]),
        ("data", [{"name": "p", "file": "/etc/passwd"}]),
    ):
        assert key not in tools_runs._ALLOWED_SCENARIO_KEYS, key
    assert {"requests", "profile", "name"} <= tools_runs._ALLOWED_SCENARIO_KEYS


def test_mcp_report_tools_read_only_inside_the_working_directory(tmp_path, monkeypatch):
    from redline_core.agent.tools_analysis import _load_report

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.json").write_text("{}")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    with pytest.raises(ValueError, match="working directory"):
        _load_report(str(outside / "report.json"))
    with pytest.raises(ValueError, match="working directory"):
        _load_report("../outside/report.json")


def test_scenario_validation_errors_do_not_echo_values():
    from redline_core.agent.tools_runs import _validation

    out = _validation(
        {"name": "x", "base_url": "ftp://secret-host-name", "requests": [{"path": "/"}]}
    )
    assert out["valid"] is False and "secret-host-name" not in json.dumps(out)
