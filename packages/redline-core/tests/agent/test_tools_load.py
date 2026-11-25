from __future__ import annotations

import io
import json
import time

import pytest

from redline_core.agent.base import ServerOptions
from redline_core.agent.mcp import serve
from redline_core.agent.tools import TOOLS, call_tool, visible_tools
from redline_core.detect import detect
from redline_core.histogram import Histogram
from redline_core.runs import Run, RunStep
from redline_core.storage.multiple import TraceReader
from redline_core.storage.sqlite import SQLiteStore

from ..conftest import make_callsite, make_trace, sql_span

SECRET = "tok-9f8e7d6c5b4a"
PASSWORD = "hunter2-pw"
TARGET = "http://127.0.0.1:8000"


@pytest.fixture
def run_store(tmp_path):
    path = str(tmp_path / "r.db")
    s = SQLiteStore(path, background=False)
    cs = make_callsite()
    t = make_trace(
        spans=[
            sql_span("SELECT * FROM items WHERE order_id = %s", [i], callsite=cs) for i in range(6)
        ],
        started_at=time.time() - 5,
    )
    t.issues = detect(t)
    t.tags["run_id"] = "run1"
    t.tags["step"] = "0"
    s.save(t)
    hist = Histogram()
    for ms in (10, 20, 30, 400):
        hist.record(ms)
    run = Run(
        id="run1",
        name="spike test",
        status="running",
        config={
            "name": "spike test",
            "base_url": f"https://svc:{PASSWORD}@shop.example.test:8443/api?token={SECRET}",
            "headers": {"Authorization": f"Bearer {SECRET}"},
            "requests": [
                {
                    "name": "list",
                    "method": "GET",
                    "path": f"/items?api_key={SECRET}&page=2",
                    "headers": {"X-Key": SECRET},
                    "json": {"password": PASSWORD},
                }
            ],
            "identities": [{"name": "alice", "headers": {"Authorization": SECRET}}],
            "auth": {"type": "bearer", "token": SECRET},
            "profile": {"mode": "stages", "stages": [{"rps": 5, "duration_s": 10}]},
        },
    )
    run.steps = [
        RunStep(step=0, target_rps=5, sent=40, ok=38, errors=2, hist=hist, achieved_rps=4.9)
    ]
    s.save_run(run)
    s.finish_run(
        "run1",
        "done",
        {
            "knee_step": 0,
            "max_ok_rps": 4.9,
            "verdict": "ok",
            "flows": {"checkout": {"count": 3}},
            "recovery": [{"after_step": 0, "recovered": True}],
            "drift": [],
            "identities": {"count": 1, "requests": 40, "by_name": {"alice": 40}},
            "message": f"failed at https://u:{PASSWORD}@db.example.test/x",
        },
    )
    for ts in (1.0, 2.0):
        s.save_run_metrics("run1", 0, {"cpu_pct": 80.0, "pool_checked_out": 5.0}, pid=1, ts=ts)
    s.close()
    return path


@pytest.fixture
def reader(run_store):
    r = TraceReader(run_store)
    yield r
    r.close()


def dump(value):
    return json.dumps(value)


def test_list_and_get_run_never_return_secrets(reader):
    listing = call_tool("list_load_runs", {}, store=reader)
    assert listing["items"][0]["id"] == "run1" and listing["items"][0]["verdict"] == "ok"
    run = call_tool("get_load_run", {"run_id": "run1"}, store=reader)
    assert run["steps"][0]["sent"] == 40 and run["steps_total"] == 1
    cfg = run["config"]
    assert cfg["base_url"] == "https://shop.example.test:8443/api"
    assert cfg["header_names"] == ["Authorization"]
    first = cfg["requests"][0]
    assert first["path"] == "/items" and first["query_names"] == ["api_key", "page"]
    assert first["has_body"] is True and "json" not in first
    assert cfg["identities"] == ["alice"]
    assert cfg["auth"] == {"configured": True, "type": "bearer"}
    for tool in ("list_load_runs", "get_load_run", "load_run_summary", "load_run_metrics"):
        args = {} if tool == "list_load_runs" else {"run_id": "run1"}
        out = dump(call_tool(tool, args, store=reader))
        assert SECRET not in out and PASSWORD not in out, tool


def test_summary_has_analysis_sections(reader):
    out = call_tool("load_run_summary", {"run_id": "run1"}, store=reader)
    assert out["status"] == "done"
    assert {"flows", "recovery", "drift", "identities"} <= set(out["sections"])
    assert out["summary"]["identities"]["by_name"] == {"alice": 40}
    assert "***@" in out["summary"]["message"]


def test_bottlenecks_and_metrics(reader):
    bn = call_tool("load_run_bottlenecks", {"run_id": "run1", "limit": 5}, store=reader)
    assert bn["steps"][0]["step"] == 0 and "items_total" in bn
    m = call_tool("load_run_metrics", {"run_id": "run1"}, store=reader)
    assert m["steps"][0]["step"] == 0 and "limiting_resource" in m


def test_unknown_run_and_bad_ids_are_errors(reader):
    for tool in ("get_load_run", "load_run_summary", "load_run_bottlenecks", "load_run_metrics"):
        assert "error" in call_tool(tool, {"run_id": "nope"}, store=reader)
        assert "error" in call_tool(tool, {"run_id": "../etc"}, store=reader)
        assert "error" in call_tool(tool, {"run_id": "x" * 65}, store=reader)
        assert "error" in call_tool(tool, {}, store=reader)


def test_metrics_on_a_store_without_samples(tmp_path):
    path = str(tmp_path / "m.db")
    s = SQLiteStore(path, background=False)
    s.save_run(Run(id="r2", name="x"))
    s.close()
    r = TraceReader(path)
    assert call_tool("load_run_metrics", {"run_id": "r2"}, store=r)["steps"] == []
    r.close()


GOOD = {
    "name": "smoke",
    "base_url": TARGET,
    "headers": {"Authorization": f"Bearer {SECRET}"},
    "requests": [{"name": "home", "method": "GET", "path": "/"}],
    "profile": {"mode": "constant", "rps": 5, "duration_s": 10},
}


def test_validate_scenario_valid_and_invalid(reader):
    ok = call_tool("validate_scenario", {"scenario": GOOD}, store=reader)
    assert ok["valid"] is True and ok["ran"] is False
    assert ok["summary"]["header_names"] == ["Authorization"] and SECRET not in dump(ok)
    bad = call_tool("validate_scenario", {"scenario": {"requests": []}}, store=reader)
    assert bad["valid"] is False and bad["errors"][0]["message"]
    unknown = call_tool("validate_scenario", {"scenario": {**GOOD, "bogus": 1}}, store=reader)
    assert unknown["valid"] is False
    assert "error" in call_tool("validate_scenario", {}, store=reader)
    both = {"scenario": GOOD, "path": "a.json"}
    assert "error" in call_tool("validate_scenario", both, store=reader)


def test_validate_scenario_file_stays_in_working_directory(reader, tmp_path, monkeypatch):
    (tmp_path / "s.json").write_text(json.dumps(GOOD))
    (tmp_path / "s.toml").write_text(
        'name = "t"\nbase_url = "http://127.0.0.1:1"\n'
        '[[requests]]\nname = "a"\npath = "/"\n'
        '[profile]\nmode = "constant"\nrps = 1\nduration_s = 5\n'
    )
    (tmp_path / "s.py").write_text("raise SystemExit('executed')\n")
    monkeypatch.chdir(tmp_path)
    assert call_tool("validate_scenario", {"path": "s.json"}, store=reader)["valid"] is True
    assert call_tool("validate_scenario", {"path": "s.toml"}, store=reader)["valid"] is True
    assert "error" in call_tool("validate_scenario", {"path": "s.py"}, store=reader)
    assert "error" in call_tool("validate_scenario", {"path": "../s.json"}, store=reader)
    assert "error" in call_tool("validate_scenario", {"path": "/etc/hosts"}, store=reader)
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert "error" in call_tool("validate_scenario", {"path": "../s.json"}, store=reader)


OPENAPI = {
    "openapi": "3.0.0",
    "info": {"title": "Shop API"},
    "servers": [{"url": "http://127.0.0.1:9000"}],
    "paths": {
        "/items": {
            "get": {
                "operationId": "listItems",
                "parameters": [
                    {"name": "X-Api-Key", "in": "header", "required": True, "example": SECRET}
                ],
            },
            "post": {"operationId": "addItem"},
        },
        "/items/{id}": {"get": {"operationId": "getItem"}},
    },
}


def test_generate_from_openapi_masks_header_values(reader):
    out = call_tool("generate_scenario_from_openapi", {"openapi": OPENAPI}, store=reader)
    assert out["ran"] is False and out["requests_total"] == 2
    assert out["validation"]["valid"] is True
    assert SECRET not in dump(out)
    weights = {r["name"]: r["weight"] for r in out["scenario"]["requests"]}
    assert weights["listItems"] == 0 and weights["getItem"] == 1  # a required header needs review
    assert "addItem" not in weights  # writes are left out unless asked for
    assert out["toml"].startswith("#")
    with_writes = call_tool(
        "generate_scenario_from_openapi", {"openapi": OPENAPI, "include_writes": True}, store=reader
    )
    assert {r["name"] for r in with_writes["scenario"]["requests"]} >= {"addItem"}


def test_generate_from_openapi_file_and_errors(reader, tmp_path, monkeypatch):
    (tmp_path / "api.json").write_text(json.dumps(OPENAPI))
    monkeypatch.chdir(tmp_path)
    out = call_tool("generate_scenario_from_openapi", {"path": "api.json"}, store=reader)
    assert out["scenario"]["requests"]
    assert "error" in call_tool(
        "generate_scenario_from_openapi", {"openapi": {"x": 1}}, store=reader
    )
    remote = {"path": "https://example.test/a.json"}
    assert "error" in call_tool("generate_scenario_from_openapi", remote, store=reader)
    bad_url = {"openapi": OPENAPI, "base_url": "ftp://x"}
    assert "error" in call_tool("generate_scenario_from_openapi", bad_url, store=reader)


def test_generate_from_traces_and_empty_store(reader, tmp_path):
    empty = str(tmp_path / "e.db")
    SQLiteStore(empty, background=False).close()
    r = TraceReader(empty)
    assert "error" in call_tool("generate_scenario_from_traces", {}, store=r)
    r.close()
    out = call_tool("generate_scenario_from_traces", {"window": "7d", "top": 3}, store=reader)
    assert out["validation"]["valid"] is True and out["ran"] is False
    assert out["scenario"]["requests"][0]["path"] == "/orders/1/"


def test_generate_from_stages_text_and_list(reader):
    out = call_tool(
        "generate_scenario_from_stages", {"stages": "10:30s,200:20s,10:60s"}, store=reader
    )
    assert out["validation"]["valid"] is True
    assert out["scenario"]["profile"]["stages"][1] == {"rps": 200.0, "duration_s": 20.0}
    listed = call_tool(
        "generate_scenario_from_stages",
        {"stages": [{"rps": 5, "duration_s": 10}], "requests": [{"name": "a", "path": "/a"}]},
        store=reader,
    )
    assert listed["validation"]["valid"] is True
    for bad in ("nonsense", 5, []):
        assert "error" in call_tool("generate_scenario_from_stages", {"stages": bad}, store=reader)


# ------------------------------------------------------------------ gated tools
GATED = {"start_load_run", "stop_load_run", "delete_load_run"}


def test_gated_tools_are_hidden_and_refused_by_default(reader):
    assert GATED <= set(TOOLS)
    assert not GATED & {t.name for t in visible_tools()}
    for name in GATED:
        out = call_tool(name, {"scenario": GOOD, "run_id": "run1"}, store=reader)
        assert "error" in out and "unknown tool" in out["error"]
    only_load = {t.name for t in visible_tools(ServerOptions(allow_load=True))}
    assert {"start_load_run", "stop_load_run"} <= only_load
    assert "delete_load_run" not in only_load


def test_gate_applies_over_the_protocol(run_store):
    def listing(options):
        out = io.StringIO()
        msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        serve(run_store, io.StringIO(msg + "\n"), out, options=options)
        return {t["name"] for t in json.loads(out.getvalue())["result"]["tools"]}

    assert not GATED & listing(None)
    everything = ServerOptions(allow_load=True, load_target=TARGET, allow_delete_runs=True)
    assert GATED <= listing(everything)


def test_start_load_run_uses_the_fixed_target(reader, run_store, monkeypatch):
    calls = []

    def fake_launch(config, base_url, store_path=None, **kw):
        calls.append((config, base_url, store_path))
        return "newrun"

    monkeypatch.setattr("redline_core.load.control.launch_background", fake_launch)
    opts = ServerOptions(store_path=run_store, allow_load=True, load_target=TARGET)
    scenario = {"scenario": {**GOOD, "base_url": "http://other.example.test"}}
    out = call_tool("start_load_run", scenario, store=reader, options=opts)
    assert out["id"] == "newrun" and out["target"] == TARGET
    assert calls[0][1] == TARGET and calls[0][2] == run_store
    for key in ("data", "identities_file", "metric_sources"):
        args = {"scenario": {**GOOD, key: "x"}}
        bad = call_tool("start_load_run", args, store=reader, options=opts)
        assert "error" in bad and key in bad["error"]
    no_target = ServerOptions(store_path=run_store, allow_load=True)
    assert "error" in call_tool(
        "start_load_run", {"scenario": GOOD}, store=reader, options=no_target
    )


def test_start_refuses_a_second_run(tmp_path):
    path = str(tmp_path / "busy.db")
    s = SQLiteStore(path, background=False)
    s.save_run(Run(id="busy", name="x", status="running"))
    s.close()
    r = TraceReader(path)
    opts = ServerOptions(store_path=path, allow_load=True, load_target=TARGET)
    out = call_tool("start_load_run", {"scenario": GOOD}, store=r, options=opts)
    assert "already running" in out["error"]
    r.close()


def test_start_load_run_rejects_an_invalid_scenario(reader, run_store):
    opts = ServerOptions(store_path=run_store, allow_load=True, load_target=TARGET)
    out = call_tool("start_load_run", {"scenario": {"requests": []}}, store=reader, options=opts)
    assert "error" in out


def test_stop_and_delete_load_run(reader, run_store, tmp_path):
    opts = ServerOptions(
        store_path=run_store, allow_load=True, load_target=TARGET, allow_delete_runs=True
    )
    assert "error" in call_tool("stop_load_run", {"run_id": "nope"}, store=reader, options=opts)
    stopped = call_tool("stop_load_run", {"run_id": "run1"}, store=reader, options=opts)
    assert stopped == {"id": "run1", "stopped": False}  # nothing alive; the stop file is written
    assert (tmp_path / "runs" / "run1.stop").exists()
    deleted = call_tool("delete_load_run", {"run_id": "run1"}, store=reader, options=opts)
    assert deleted == {"id": "run1", "deleted": 1}
    assert "error" in call_tool("get_load_run", {"run_id": "run1"}, store=reader)


def test_delete_refuses_a_running_run(tmp_path):
    path = str(tmp_path / "d.db")
    s = SQLiteStore(path, background=False)
    s.save_run(Run(id="live", name="x", status="running"))
    s.close()
    r = TraceReader(path)
    opts = ServerOptions(store_path=path, allow_delete_runs=True)
    out = call_tool("delete_load_run", {"run_id": "live"}, store=r, options=opts)
    assert "stop the run" in out["error"]
    r.close()


def test_cli_load_flags_need_a_valid_target(run_store, tmp_path, capsys):
    from redline_core import cli

    assert cli.main(["mcp", "--store", run_store, "--allow-load"]) == 2
    assert "--load-target" in capsys.readouterr().err
    assert cli.main(["mcp", "--store", run_store, "--allow-load", "--load-target", "ftp://x"]) == 2
    assert "http" in capsys.readouterr().err
