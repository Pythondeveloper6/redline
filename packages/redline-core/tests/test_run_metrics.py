"""Resource metrics sampled by the application during a run, and the limiting resource."""

from __future__ import annotations

import time

import pytest

from redline_core import run_metrics as rm
from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.recorder import Recorder
from redline_core.runs import Run
from redline_core.storage import SQLiteStore


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(str(tmp_path / "m.db"), background=False)
    yield s
    s.close()


def sample(step, **values):
    return {"step": step, "ts": time.time(), "pid": 1, **values}


# ---------------------------------------------------------------- storage
def test_samples_round_trip_and_follow_the_run(store):
    store.save_run(Run(id="r1", name="x", status="running"))
    store.save_run_metrics(
        "r1", 1, {"cpu_pct": 40.5, "rss_mb": 120, "bad": "x", "nan": float("nan")}, pid=7
    )
    store.save_run_metrics("r1", 2, {"cpu_pct": 90}, pid=7)
    got = store.run_metric_samples("r1")
    assert [(s["step"], s["pid"]) for s in got] == [(1, 7), (2, 7)]
    assert got[0]["cpu_pct"] == 40.5 and "bad" not in got[0] and "nan" not in got[0]
    store.save_run_metrics("r1", 1, {"only_text": "x"})  # nothing numeric: nothing stored
    assert len(store.run_metric_samples("r1")) == 2
    store.delete_run("r1")
    assert store.run_metric_samples("r1") == []


def test_samples_per_run_are_capped(store):
    store.MAX_METRIC_SAMPLES = 5
    for i in range(12):
        store.save_run_metrics("r2", 1, {"cpu_pct": i}, ts=1000.0 + i)
    kept = store.run_metric_samples("r2")
    assert len(kept) == 5 and kept[-1]["cpu_pct"] == 11


# ---------------------------------------------------------------- analysis
def test_aggregate_per_step():
    out = rm.aggregate([sample(1, cpu_pct=10), sample(1, cpu_pct=30), sample(2, cpu_pct=80)])
    assert [s["step"] for s in out] == [1, 2]
    assert out[0]["metrics"]["cpu_pct"] == {"avg": 20.0, "max": 30.0}
    assert out[0]["samples"] == 2 and out[0]["workers"] == 1


def steps(*rows):
    return rm.aggregate([sample(i + 1, **r) for i, r in enumerate(rows)])


def test_a_pool_of_five_with_waiting_requests_is_the_limit():
    per_step = steps(
        {"pool_capacity": 5, "pool_checked_out": 1, "pool_wait_ms": 0.0, "cpu_pct": 20},
        {"pool_capacity": 5, "pool_checked_out": 5, "pool_wait_ms": 40.0, "cpu_pct": 30},
    )
    verdict = rm.limiting_resource(per_step, 2)
    assert verdict["resource"] == "pool" and verdict["step"] == 2
    assert "connection pool exhausted (5/5 checked out)" in verdict["detail"]
    assert "wait time 40x" in verdict["detail"]


def test_cpu_and_gc_and_unknown():
    cpu = steps({"cpu_pct": 20}, {"cpu_pct": 96})
    assert rm.limiting_resource(cpu, 2)["resource"] == "cpu"
    gc_heavy = steps({"cpu_pct": 20}, {"cpu_pct": 50, "gc_pause_ms": 180})
    assert rm.limiting_resource(gc_heavy, 2)["resource"] == "gc"
    calm = steps({"cpu_pct": 20}, {"cpu_pct": 30})
    assert rm.limiting_resource(calm, 2)["resource"] == "unknown"
    assert rm.limiting_resource([], None) is None


def test_a_pool_that_is_full_but_not_waited_on_is_not_blamed():
    per_step = steps({"pool_capacity": 5, "pool_checked_out": 5, "pool_wait_ms": 0.5})
    assert rm.limiting_resource(per_step, 1)["resource"] == "unknown"


def test_memory_growth_is_a_note_not_the_limit():
    per_step = steps({"rss_mb": 100, "cpu_pct": 10}, {"rss_mb": 180, "cpu_pct": 20})
    verdict = rm.limiting_resource(per_step, 2)
    assert verdict["resource"] == "unknown" and "memory grew from 100 to 180 MB" in verdict["note"]


# ---------------------------------------------------------------- sampler
def test_the_sampler_only_samples_a_registered_running_run(store):
    store.save_run(Run(id="live", name="x", status="running"))
    sampler = rm.RunMetricsSampler(store, interval=0.05)
    sampler.note("made-up", "1")  # not in the store: ignored
    sampler.note("live", "probe")  # the recording probe is not a step
    assert sampler._thread is None
    sampler.note("live", "2")
    deadline = time.time() + 5
    while time.time() < deadline and not store.run_metric_samples("live"):
        time.sleep(0.05)
    got = store.run_metric_samples("live")
    assert got and got[0]["step"] == 2 and "cpu_pct" in got[0] and got[0]["threads"] >= 1
    assert store.run_metric_samples("made-up") == []


def test_the_recorder_starts_sampling_for_tagged_requests(store):
    store.save_run(Run(id="rec", name="x", status="running"))
    rec = Recorder(Settings(), store)
    sampler = rm.sampler_for(store)
    sampler.interval = 0.05
    with rec.recording(method="GET", path="/a", headers={"X-Redline-Run": "rec;step=3"}):
        pass
    deadline = time.time() + 5
    while time.time() < deadline and not store.run_metric_samples("rec"):
        time.sleep(0.05)
    assert store.run_metric_samples("rec")[0]["step"] == 3


def test_run_metrics_can_be_switched_off(store):
    store.save_run(Run(id="off", name="x", status="running"))
    rec = Recorder(Settings(run_metrics=False), store)
    with rec.recording(method="GET", path="/a", headers={"X-Redline-Run": "off;step=1"}):
        pass
    time.sleep(0.3)
    assert store.run_metric_samples("off") == []


# ---------------------------------------------------------------- API and sources
def test_api_reports_steps_and_the_limiting_resource(store):
    store.save_run(Run(id="a1", name="x", status="done", summary={"knee_step": 2}))
    store.save_run_metrics(
        "a1", 1, {"pool_capacity": 5, "pool_checked_out": 1, "pool_wait_ms": 0.0}
    )
    store.save_run_metrics(
        "a1", 2, {"pool_capacity": 5, "pool_checked_out": 5, "pool_wait_ms": 30.0}
    )
    status, payload = RedlineAPI(store, Settings()).dispatch("GET", "api/runs/a1/metrics")
    assert status == 200 and len(payload["steps"]) == 2
    assert payload["limiting_resource"]["resource"] == "pool"
    assert RedlineAPI(store, Settings()).dispatch("GET", "api/runs/none/metrics")[0] == 404


def test_remote_generators_can_add_metric_source_numbers(tmp_path):
    store = SQLiteStore(str(tmp_path / "r.db"), background=False)
    api = RedlineAPI(store, Settings(accept_remote_runs=True))
    body = {"id": "rr", "name": "x", "created_at": 1.0, "config": {}}
    assert api.dispatch("POST", "api/runs/register", body=body)[0] == 201
    status, payload = api.dispatch(
        "POST", "api/runs/rr/metrics", body={"step": 1, "values": {"db_cpu_pct": 71.5}}
    )
    assert status == 200 and payload["stored"] == 1
    assert store.run_metric_samples("rr")[0]["db_cpu_pct"] == 71.5
    bad = api.dispatch("POST", "api/runs/rr/metrics", body={"step": 1, "values": {"x": "no"}})
    assert bad[0] == 400
    store.close()


def test_metric_sources_run_per_step_and_failures_do_not_stop_the_run():
    from redline_core.load.analysis import StepResult

    saved = []
    calls = []

    import sys
    import types

    module = types.ModuleType("fake_metric_source")

    def good(start, end):
        calls.append((start, end))
        return {"db_cpu_pct": 55.0}

    def bad(start, end):
        raise RuntimeError("monitor down")

    module.good, module.bad = good, bad
    sys.modules["fake_metric_source"] = module
    steps_ = [StepResult(step=i, target_rps=5, started_at=i * 10.0, ended_at=i * 10.0 + 9,
                         duration_s=9) for i in (1, 2)]  # fmt: skip
    failed = rm.run_sources(
        ["fake_metric_source:good", "fake_metric_source:bad", "nope.missing:fn"],
        steps_,
        lambda step, values, ts: saved.append((step, values, ts)),
    )
    assert [s[0] for s in saved] == [1, 2] and saved[0][1] == {"db_cpu_pct": 55.0}
    assert calls == [(10.0, 19.0), (20.0, 29.0)]
    assert sorted(failed) == ["fake_metric_source:bad", "nope.missing:fn"]
    del sys.modules["fake_metric_source"]


def test_a_run_summary_carries_the_resources_and_the_limit(store, tmp_path):
    from redline_core.load import engine
    from redline_core.load.scenario import parse_scenario
    from redline_core.load.sinks import StoreSink
    from tests.load.conftest import _start  # the small local HTTP server of the load tests

    server, _, url = _start(threaded=True)
    try:
        sc = parse_scenario({"name": "m", "base_url": url, "requests": [{"path": "/a"}]})
        sink = StoreSink(store)
        for step, checked, wait in ((1, 1, 0.0), (2, 5, 35.0)):
            store.save_run_metrics(
                "fixed-id",
                step,
                {"pool_capacity": 5, "pool_checked_out": checked, "pool_wait_ms": wait},
            )
        result = engine.run(
            sc,
            profile={"stages": [{"rps": 5, "duration_s": 2}, {"rps": 10, "duration_s": 2}]},
            sink=sink,
            workers=1,
            run_id="fixed-id",
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.summary["limiting_resource"]["resource"] == "pool"
    assert [s["step"] for s in result.summary["resources"]] == [1, 2]
