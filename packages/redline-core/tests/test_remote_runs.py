"""Register a load run over HTTP (``POST api/runs/register`` and friends)."""

from __future__ import annotations

import base64

import pytest

from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.histogram import Histogram
from redline_core.storage import SQLiteStore


@pytest.fixture
def api(tmp_path):
    store = SQLiteStore(tmp_path / "r.db", background=False)
    yield RedlineAPI(store, Settings(accept_remote_runs=True))
    store.close()


def hist_b64(*values):
    h = Histogram()
    for v in values:
        h.record(v)
    return base64.b64encode(h.to_bytes()).decode()


def step_body(**extra):
    body = {
        "step": 1, "target_rps": 10.0, "started_at": 100.0, "ended_at": 110.0,
        "achieved_rps": 9.5, "sent": 100, "ok": 98, "errors": 2, "dropped": 0,
        "hist": hist_b64(5.0, 7.0, 9.0), "error_breakdown": {"500": 2},
    }  # fmt: skip
    body.update(extra)
    return body


def register(api, run_id="r1", **extra):
    body = {"id": run_id, "name": "remote", "created_at": 100.0, "config": {"requests": []}}
    body.update(extra)
    return api.dispatch("POST", "api/runs/register", body=body)


def test_register_step_finish_round_trip(api):
    assert register(api)[0] == 201
    assert api.dispatch("POST", "api/runs/r1/steps", body=step_body())[0] == 200
    status, _ = api.dispatch(
        "POST", "api/runs/r1/finish", body={"status": "done", "summary": {"verdict": "ok"}}
    )
    assert status == 200
    run = api.store.get_run("r1")
    assert run.status == "done" and len(run.steps) == 1
    assert run.steps[0].sent == 100 and run.steps[0].error_breakdown == {"500": 2}
    assert run.steps[0].hist.percentile(50) > 0
    assert api.dispatch("GET", "api/runs/r1")[1]["status"] == "done"


def test_off_by_default(tmp_path):
    store = SQLiteStore(tmp_path / "r.db", background=False)
    api = RedlineAPI(store, Settings())
    status, payload = register(api)
    assert status == 403 and payload["error"]["code"] == "remote_runs_disabled"
    store.close()


def test_register_validates_the_body(api):
    assert register(api, run_id="bad id!")[0] == 400
    assert register(api, run_id="register")[0] == 400
    assert register(api, config="nope")[0] == 400
    assert api.dispatch("POST", "api/runs/register", body={"name": "x"})[0] == 400


def test_register_twice_is_a_conflict(api):
    assert register(api)[0] == 201
    assert register(api)[0] == 409


def test_register_masks_secrets_in_the_config(api):
    config = {"headers": {"Authorization": "Bearer top-secret"}, "requests": []}
    assert register(api, config=config)[0] == 201
    assert "top-secret" not in str(api.store.get_run("r1").config)


def test_only_remote_runs_can_be_changed_over_http(api):
    from redline_core.runs import Run

    api.store.save_run(Run(id="local1", name="local", status="running"))
    assert api.dispatch("POST", "api/runs/local1/steps", body=step_body())[0] == 403
    assert api.dispatch("POST", "api/runs/local1/finish", body={"status": "done"})[0] == 403
    assert api.store.get_run("local1").status == "running"


def test_unknown_run_is_404_and_finished_run_is_closed(api):
    assert api.dispatch("POST", "api/runs/nope/steps", body=step_body())[0] == 404
    register(api)
    api.dispatch("POST", "api/runs/r1/finish", body={"status": "done"})
    assert api.dispatch("POST", "api/runs/r1/steps", body=step_body())[0] == 409
    assert api.dispatch("POST", "api/runs/r1/finish", body={"status": "failed"})[0] == 409


@pytest.mark.parametrize(
    "bad",
    [
        {"step": -1},
        {"step": "1"},
        {"sent": -5},
        {"ok": True},
        {"hist": "not base64 !!"},
        {"hist": base64.b64encode(b"junk").decode()},
        {"error_breakdown": {"x": "many"}},
        {"error_breakdown": {str(i): 1 for i in range(500)}},
        {"target_rps": float("nan")},
    ],
)
def test_step_validation(api, bad):
    register(api)
    assert api.dispatch("POST", "api/runs/r1/steps", body=step_body(**bad))[0] == 400


def test_finish_status_must_be_known(api):
    register(api)
    assert api.dispatch("POST", "api/runs/r1/finish", body={"status": "running"})[0] == 400
    assert api.dispatch("POST", "api/runs/r1/finish", body={"status": "weird"})[0] == 400


# ------------------------------------------------------------------ the generator side
def _client(api, token="tok-123"):
    import httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path.removeprefix("/_redline/")
        import json as _json

        body = _json.loads(request.content) if request.content else None
        status, payload = api.dispatch(request.method, path, body=body)
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_http_sink_registers_a_whole_run(api):
    from redline_core.load.analysis import StepResult
    from redline_core.load.sinks import HttpSink, RunMeta

    client, seen = _client(api)
    sink = HttpSink("http://app/_redline", "tok-123", client=client)
    sink.preflight()
    sink.start(RunMeta(id="g1", name="gen", created_at=50.0, config={"requests": []}))
    h = Histogram()
    h.record(12.0)
    sink.step(
        "g1",
        StepResult(step=1, target_rps=5, started_at=50.0, ended_at=60.0, duration_s=10.0,
                   sent=10, ok=10, hist=h),
    )  # fmt: skip
    sink.finish("g1", "done", {"verdict": "ok"})
    run = api.store.get_run("g1")
    assert run.status == "done" and run.steps[0].ok == 10
    assert all(r.headers["authorization"] == "Bearer tok-123" for r in seen)


def test_http_sink_reports_a_disabled_application_without_the_token(tmp_path):
    from redline_core.load.sinks import HttpSink, RegisterError, RunMeta

    store = SQLiteStore(tmp_path / "x.db", background=False)
    off = RedlineAPI(store, Settings())
    client, _ = _client(off)
    sink = HttpSink("http://app/_redline", "tok-secret-999", client=client)
    with pytest.raises(RegisterError) as info:
        sink.start(RunMeta(id="g2", name="gen", created_at=1.0))
    assert "accept_remote_runs" in str(info.value)
    assert "tok-secret-999" not in str(info.value)
    store.close()


def test_read_only_refuses_remote_registration(tmp_path):
    store = SQLiteStore(tmp_path / "x.db", background=False)
    ro = RedlineAPI(store, Settings(accept_remote_runs=True, read_only=True))
    assert register(ro)[0] == 403
    store.close()


def test_http_sink_finds_a_tagged_trace_through_the_api(api):
    from redline_core.load.sinks import HttpSink

    from .conftest import make_trace

    client, _ = _client(api)
    sink = HttpSink("http://app/_redline", "tok", client=client)
    assert sink.has_run_trace("g9") is False
    trace = make_trace()
    trace.tags["run_id"] = "g9"
    api.store.save(trace)
    api.store.flush()
    assert sink.has_run_trace("g9") is True
