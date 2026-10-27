"""``redline`` CLI: load runs, Ctrl-C, background runs, ``load init`` generators."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from redline_core import cli
from redline_core.load import launch_background, load_scenario, runs_dir, stop_run
from redline_core.load.sinks import StoreSink
from redline_core.storage import SQLiteStore

from ..conftest import make_trace
from .test_schedule_analysis import FakeStore


def write_scenario(tmp_path, url, **extra):
    path = tmp_path / "scenario.json"
    data = {"name": "cli", "base_url": url, "requests": [{"path": "/a"}], **extra}
    path.write_text(json.dumps(data))
    return str(path)


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------- fast
def test_help_and_no_command(capsys):
    assert cli.main([]) == 2
    assert "load" in capsys.readouterr().out
    with pytest.raises(SystemExit) as info:
        cli.main(["load", "--help"])
    assert info.value.code == 0
    assert "--fail-p95-ms" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--steps", "1:2"], "A:B:STEP"),
        (["--ramp", "x:2"], "A:B"),
        (["--workers", "0"], "--workers"),
        (["--base-url", "ftp://x"], "--base-url"),
    ],
)
def test_usage_errors_exit_2(tmp_path, capsys, args, message):
    sc = write_scenario(tmp_path, "http://127.0.0.1:1")
    assert cli.main(["load", sc, "--no-store", *args]) == 2
    assert message in capsys.readouterr().err


def test_bad_scenario_exits_2(tmp_path, capsys):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"requests": [{"path": "/", "wieght": 2}]}))
    assert cli.main(["load", str(path), "--no-store"]) == 2
    assert "requests[0].wieght: unknown key; did you mean 'weight'?" in capsys.readouterr().err
    path.write_text(json.dumps({"requests": [{"path": "/"}]}))
    assert cli.main(["load", str(path), "--no-store"]) == 2
    assert "base_url: is required" in capsys.readouterr().err


def test_cli_profile_overrides(tmp_path):
    sc = load_scenario(write_scenario(tmp_path, "http://h", profile={"stop_on": {"p95_ms": 9}}))
    parser = cli._parser()
    args = parser.parse_args(["load", "x", "--steps", "10:30:10", "--step-duration", "2s"])
    prof = cli._cli_profile(args, sc)
    assert [p.target_rps for p in prof.plan()] == [10, 20, 30]
    assert prof.stop_on.p95_ms == 9
    args = parser.parse_args(["load", "x", "--ramp", "0:10", "--duration", "1m", "--poisson"])
    prof = cli._cli_profile(args, sc)
    assert prof.mode == "ramp" and prof.duration_s == 60 and prof.poisson
    args = parser.parse_args(["load", "x", "--rps", "7"])
    assert cli._cli_profile(args, sc).plan()[0].target_rps == 7


OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Shop"},
    "servers": [{"url": "/api"}],
    "paths": {
        "/orders": {
            "get": {"operationId": "list_orders"},
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Order"}}
                    }
                }
            },
        },
        "/orders/{order_id}": {
            "parameters": [
                {
                    "name": "order_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "integer", "example": 42},
                }
            ],
            "get": {"operationId": "get_order"},
            "delete": {},
        },
        "/users/{uid}/items/{slug}": {
            "get": {
                "parameters": [
                    {
                        "name": "uid",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    },
                    {
                        "name": "slug",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "enum": ["red", "blue"]},
                    },
                ]
            }
        },
        "/search": {
            "get": {
                "parameters": [
                    {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "page", "in": "query", "schema": {"type": "integer"}},
                ]
            }
        },
        "/plain": {"get": {}},
    },
    "components": {
        "schemas": {
            "Order": {
                "type": "object",
                "required": ["sku"],
                "properties": {
                    "sku": {"type": "string", "example": "A-1"},
                    "qty": {"type": "integer"},
                },
            }
        }
    },
}


def test_init_from_openapi(tmp_path, capsys):
    spec = tmp_path / "openapi.json"
    spec.write_text(json.dumps(OPENAPI))
    out = tmp_path / "scenario.toml"
    assert cli.main(["load", "init", "--from-openapi", str(spec), "-o", str(out)]) == 0
    assert "5 requests (4 enabled)" in capsys.readouterr().out
    if sys.version_info >= (3, 11):
        sc = load_scenario(out)
    else:  # pragma: no cover
        return
    by_name = {r.name: r for r in sc.requests}
    assert sc.base_url == "http://127.0.0.1:8000/api" and sc.name == "Shop"
    assert by_name["get_order"].path == "/orders/42"
    assert by_name["GET /users/{uid}/items/{slug}"].path == (
        "/users/00000000-0000-4000-8000-000000000001/items/red"
    )
    search = by_name["GET /search"]
    assert search.path == "/search?q=example" and search.weight == 0  # required query: review
    assert all(r.method == "GET" for r in sc.requests)
    # existing file is not overwritten without --force
    assert cli.main(["load", "init", "--from-openapi", str(spec), "-o", str(out)]) == 2


def test_init_from_openapi_with_writes_to_stdout(tmp_path, capsys):
    spec = tmp_path / "openapi.json"
    spec.write_text(json.dumps(OPENAPI))
    args = ["load", "init", "--from-openapi", str(spec), "--include-writes", "-o", "-"]
    assert cli.main([*args, "--base-url", "http://shop:9"]) == 0
    text = capsys.readouterr().out
    assert "[[requests]]" in text  # default format is TOML
    out = tmp_path / "s.json"
    assert cli.main([*args[:-1], str(out), "--base-url", "http://shop:9"]) == 0
    data = json.loads(out.read_text())
    writes = [r for r in data["requests"] if r["method"] != "GET"]
    assert {(r["method"], r["path"], r["weight"]) for r in writes} == {
        ("POST", "/orders", 0),
        ("DELETE", "/orders/42", 0),
    }
    assert next(r for r in writes if r["method"] == "POST")["json"] == {"sku": "A-1"}
    assert data["base_url"] == "http://shop:9"


def test_init_from_traces(tmp_path, capsys):
    db = tmp_path / ".redline" / "redline.db"
    store = SQLiteStore(str(db), background=False)
    for i in range(5):
        store.save(make_trace(path=f"/orders/{i}/", route="/orders/<int:pk>/"))
    for _ in range(2):
        store.save(make_trace(path="/health", route=None))
    store.save(make_trace(path="/cart/", route="/cart/", method="POST"))
    store.save(make_trace(path="/_redline/api/meta", route="/_redline/api/meta"))
    store.close()
    out = tmp_path / "s.json"
    assert cli.main(["load", "init", "--from-traces", "--store", str(db), "-o", str(out)]) == 0
    data = json.loads(out.read_text())
    reqs = {r["name"]: r for r in data["requests"]}
    assert set(reqs) == {"GET /orders/<int:pk>/", "GET /health"}
    assert reqs["GET /orders/<int:pk>/"]["weight"] == 5
    assert reqs["GET /orders/<int:pk>/"]["path"].startswith("/orders/")
    assert reqs["GET /health"] == {
        "name": "GET /health",
        "method": "GET",
        "path": "/health",
        "weight": 2,
    }
    assert cli.main(["load", "init", "--from-traces", "--store", str(tmp_path / "x.db")]) == 2


# ---------------------------------------------------------------------------- slow
@pytest.mark.slow
def test_cli_run_json_and_threshold(tmp_path, capsys, http_server):
    state, url = http_server
    sc = write_scenario(tmp_path, url)
    out = tmp_path / "out" / "run.json"
    code = cli.main(
        [
            "load",
            sc,
            "--steps",
            "10:20:10",
            "--step-duration",
            "1s",
            "--workers",
            "2",
            "--no-store",
            "--json",
            str(out),
            "--fail-p95-ms",
            "0.001",
            "--name",
            "ci",
        ]
    )
    assert code == 1
    stdout = capsys.readouterr().out
    assert "FAIL: p95" in stdout and "verdict:" in stdout
    doc = json.loads(out.read_text())
    assert doc["status"] == "done" and doc["run"]["name"] == "ci"
    assert [s["sent"] for s in doc["steps"]] == [10, 20]
    assert len(state.headers) == 30


@pytest.mark.slow
def test_cli_store_sink_and_ui_hint(tmp_path, capsys, http_server, monkeypatch):
    _, url = http_server
    fake = FakeStore()
    monkeypatch.setattr(cli, "_open_store_sink", lambda path, err: StoreSink(fake))
    sc = write_scenario(tmp_path, url)
    store = tmp_path / ".redline" / "redline.db"
    code = cli.main(
        [
            "load",
            sc,
            "--rps",
            "10",
            "--duration",
            "1s",
            "--workers",
            "1",
            "--store",
            str(store),
            "--run-id",
            "abc123",
            "--fail-error-rate",
            "1%",
        ]
    )
    assert code == 0
    assert [c[0] for c in fake.calls] == ["save_run", "save_run_step", "finish_run"]
    assert fake.calls[0][1].id == "abc123"
    assert f"{url}/_redline/#/load/abc123" in capsys.readouterr().out
    rdir = runs_dir(store)
    assert not (rdir / "abc123.pid").exists() and not (rdir / "abc123.stop").exists()


@pytest.mark.slow
def test_cli_store_without_run_support_falls_back_to_json(
    tmp_path, capsys, http_server, monkeypatch
):
    _, url = http_server
    from redline_core.load import sinks

    monkeypatch.setattr(sinks, "STORE_RUN_METHODS", ("save_run_that_does_not_exist",))
    sc = write_scenario(tmp_path, url)
    store = tmp_path / ".redline" / "redline.db"
    code = cli.main(
        [
            "load",
            sc,
            "--rps",
            "5",
            "--duration",
            "1s",
            "--workers",
            "1",
            "--store",
            str(store),
            "--run-id",
            "r1",
            "--quiet",
        ]
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "cannot hold load runs" in err
    doc = json.loads((runs_dir(store) / "r1.json").read_text())
    assert doc["status"] == "done" and doc["steps"][0]["sent"] == 5


def _env():
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    return env


@pytest.mark.slow
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_ctrl_c_stops_gracefully(tmp_path, http_server):
    state, url = http_server
    sc = write_scenario(tmp_path, url)
    out = tmp_path / "run.json"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "redline_core.cli",
            "load",
            sc,
            "--rps",
            "20",
            "--duration",
            "30s",
            "--workers",
            "2",
            "--no-store",
            "--json",
            str(out),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_env(),
        text=True,
    )
    try:
        assert wait_for(lambda: len(state.headers) >= 20)
        proc.send_signal(signal.SIGINT)
        output, _ = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130, output
    doc = json.loads(out.read_text())
    assert doc["status"] == "stopped"
    assert doc["summary"]["stopped_reason"] == "interrupted (Ctrl-C)"
    (step,) = doc["steps"]
    assert step["partial"] and step["sent"] >= 20 and step["sent"] == len(state.headers)


@pytest.mark.slow
def test_launch_background_and_stop_run(tmp_path, http_server):
    state, url = http_server
    store = tmp_path / ".redline" / "redline.db"
    run_id = launch_background(
        {
            "name": "ui",
            "requests": [{"method": "GET", "path": "/a", "weight": 1}],
            "profile": {"mode": "constant", "rps": 10, "duration_s": 60},
        },
        url,
        store,
        workers=1,
    )
    rdir = runs_dir(store)
    assert (rdir / f"{run_id}.scenario.json").exists()
    assert wait_for(lambda: len(state.headers) >= 5)
    assert all(h.startswith(f"{run_id};step=1") for h in state.headers)
    assert stop_run(run_id, store) is True
    assert wait_for(lambda: not (rdir / f"{run_id}.pid").exists(), timeout=20), (
        rdir / f"{run_id}.log"
    ).read_text()
    results = rdir / f"{run_id}.json"
    if not hasattr(SQLiteStore, "save_run"):  # the store cannot hold runs → JSON fallback
        doc = json.loads(results.read_text())
        assert doc["status"] == "stopped" and doc["steps"][0]["partial"]
    assert stop_run(run_id, store) is False  # nothing running any more


def test_launch_background_validates_first(tmp_path):
    from redline_core.load import ScenarioError

    with pytest.raises(ScenarioError, match="requests"):
        launch_background({"requests": []}, "http://127.0.0.1:1", tmp_path / "r.db")
    with pytest.raises(ValueError, match="invalid run id"):
        stop_run("../etc", tmp_path / "r.db")


def test_import_redline_core_does_not_import_the_load_tester():
    code = (
        "import sys, redline_core; "
        "assert 'redline_core.load' not in sys.modules and 'httpx' not in sys.modules; "
        "import redline_core.load; assert 'httpx' not in sys.modules; "
        "import redline_core.cli; print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ok"
