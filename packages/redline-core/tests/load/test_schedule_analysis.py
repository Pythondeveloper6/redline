"""Schedule math, knee detection, thresholds and sinks – no processes, no network."""

from __future__ import annotations

import base64
import itertools
import json
import math
import random
import statistics

import pytest

from redline_core.histogram import Histogram
from redline_core.load import (
    JsonSink,
    MemorySink,
    MultiSink,
    Profile,
    RunMeta,
    StepResult,
    StoreSink,
    analyze,
    arrival_offsets,
    check_thresholds,
)
from redline_core.runs import Run, RunStep


def total_arrivals(rate, duration, workers, **kw):
    return sum(
        len(list(arrival_offsets(rate, duration, worker=k, workers=workers, **kw)))
        for k in range(workers)
    )


@pytest.mark.parametrize("workers", [1, 2, 3, 8])
@pytest.mark.parametrize(("rate", "duration"), [(10, 5), (7.5, 2), (0.5, 3), (100, 1.0)])
def test_fixed_arrivals_union_is_exact(rate, duration, workers):
    assert total_arrivals(rate, duration, workers) == math.ceil(rate * duration - 1e-9)
    merged = sorted(
        t
        for k in range(workers)
        for t in arrival_offsets(rate, duration, worker=k, workers=workers)
    )
    gaps = {round(b - a, 9) for a, b in itertools.pairwise(merged)}
    assert gaps <= {round(1 / rate, 9)}


def test_poisson_mean_rate():
    rng = random.Random(42)
    counts = [len(list(arrival_offsets(50, 10, poisson=True, rng=rng))) for _ in range(40)]
    assert statistics.mean(counts) == pytest.approx(500, rel=0.03)
    offsets = list(arrival_offsets(50, 200, poisson=True, rng=rng))
    gaps = [b - a for a, b in itertools.pairwise(offsets)]
    assert statistics.mean(gaps) == pytest.approx(1 / 50, rel=0.05)
    assert statistics.stdev(gaps) == pytest.approx(1 / 50, rel=0.1)  # exponential: sd = mean


def test_no_arrivals_for_zero_rate():
    assert list(arrival_offsets(0, 10)) == []


def step_counts(profile, workers=2):
    return [total_arrivals(p.target_rps, p.duration_s, workers) for p in profile.plan()]


def test_constant_plan():
    prof = Profile.constant(20, 3)
    assert [(p.index, p.target_rps, p.start_s, p.duration_s) for p in prof.plan()] == [
        (1, 20.0, 0.0, 3.0)
    ]
    assert step_counts(prof) == [60]


def test_ramp_plan_integrates_the_ramp():
    prof = Profile.ramp(10, 50, 10, step_duration_s=5)
    plans = prof.plan()
    assert [p.target_rps for p in plans] == [20.0, 40.0]
    assert sum(step_counts(prof)) == 300  # ∫ 10→50 over 10 s
    uneven = Profile.ramp(0, 30, 7, step_duration_s=5).plan()
    assert [p.duration_s for p in uneven] == [5.0, 2.0]
    assert uneven[1].start_s == 5.0


def test_steps_plan():
    prof = Profile.steps_of(10, 40, 10, step_duration_s=2)
    plans = prof.plan()
    assert [p.target_rps for p in plans] == [10, 20, 30, 40]
    assert [p.start_s for p in plans] == [0, 2, 4, 6]
    assert step_counts(prof, workers=3) == [20, 40, 60, 80]
    assert prof.total_duration_s == 8
    capped = Profile.steps_of(10, 40, 10, step_duration_s=2, duration_s=5).plan()
    assert [p.duration_s for p in capped] == [2, 2, 1]


def step(n, target, *, p95=10.0, ok=None, errors=0, dropped=0, duration=1.0, partial=False):
    ok = int(target * duration) - errors if ok is None else ok
    h = Histogram([p95 * 0.5] * 90 + [p95] * 10)
    return StepResult(
        step=n,
        target_rps=target,
        started_at=1000.0 + n,
        ended_at=1001.0 + n,
        duration_s=duration,
        sent=ok + errors,
        ok=ok,
        errors=errors,
        dropped=dropped,
        hist=h,
        partial=partial,
    )


def test_no_knee_is_healthy():
    s = analyze([step(1, 10), step(2, 20, p95=15), step(3, 30, p95=19)])
    assert s["knee_step"] is None and s["verdict"] == "healthy"
    assert s["max_ok_rps"] == 30


def test_knee_on_p95():
    s = analyze([step(1, 10), step(2, 20, p95=19), step(3, 30, p95=25), step(4, 40, p95=90)])
    assert s["knee_step"] == 3 and s["max_ok_rps"] == 20 and s["verdict"] == "saturated"
    assert "p95" in s["knee_reason"]


def test_knee_on_errors_and_achieved():
    s = analyze([step(1, 10), step(2, 100, errors=2)])
    assert s["knee_step"] == 2 and "error rate" in s["knee_reason"]
    s = analyze([step(1, 10), step(2, 100, ok=80)])  # 80 / 100 achieved
    assert s["knee_step"] == 2 and "achieved" in s["knee_reason"]
    assert analyze([step(1, 100, ok=95)])["knee_step"] is None  # 95 % is fine


def test_knee_on_first_step_is_failing():
    s = analyze([step(1, 10, errors=5), step(2, 20)])
    assert s["knee_step"] == 1 and s["max_ok_rps"] is None and s["verdict"] == "failing"


def test_partial_steps_never_decide_the_knee():
    s = analyze([step(1, 10), step(2, 20, ok=3, duration=1.0, partial=True)])
    assert s["knee_step"] is None and s["max_ok_rps"] == 10
    assert analyze([])["verdict"] == "no data"


def test_thresholds():
    s = analyze([step(1, 10, p95=200, errors=1)])
    assert check_thresholds(s) == []
    assert check_thresholds(s, fail_p95_ms=500, fail_error_rate=0.2) == []
    breaches = check_thresholds(s, fail_p95_ms=100, fail_error_rate=0.05)
    assert len(breaches) == 2 and "p95" in breaches[0] and "error rate" in breaches[1]


def test_step_to_dict_has_the_api_step_shape():
    d = step(1, 10).to_dict()
    for key in (
        "step target_rps achieved_rps sent ok errors dropped p50_ms p95_ms p99_ms max_ms "
        "error_rate error_breakdown"
    ).split():
        assert key in d


class FakeStore:
    def __init__(self):
        self.calls = []

    def save_run(self, run):
        self.calls.append(("save_run", run))

    def save_run_step(self, run_id, row):
        self.calls.append(("save_run_step", run_id, row))

    def finish_run(self, run_id, status, summary):
        self.calls.append(("finish_run", run_id, status, summary))

    def close(self):
        self.calls.append(("close",))


def test_store_sink_writes_run_rows():
    store = FakeStore()
    sink = StoreSink(store, close=True)
    meta = RunMeta(id="r1", name="n", created_at=5.0, config={"a": 1})
    sink.start(meta)
    st = step(1, 10)
    sink.step("r1", st)
    sink.finish("r1", "done", {"knee_step": None})
    (_, run), (_, rid, row), finish, close = store.calls
    assert isinstance(run, Run)
    assert (run.id, run.name, run.created_at, run.status, run.config) == (
        "r1",
        "n",
        5.0,
        "running",
        {"a": 1},
    )
    assert rid == "r1" and isinstance(row, RunStep)
    assert (row.step, row.target_rps, row.sent) == (st.step, st.target_rps, st.sent)
    assert row.hist == st.hist
    assert finish == ("finish_run", "r1", "done", {"knee_step": None})
    assert close == ("close",)
    with pytest.raises(TypeError):
        StoreSink(object())


def test_json_and_multi_sink(tmp_path):
    out = tmp_path / "sub" / "run.json"
    mem = MemorySink()

    class Broken:
        def start(self, meta):
            raise RuntimeError("boom")

        step = finish = start

    sink = MultiSink([JsonSink(out), Broken(), mem])
    sink.start(RunMeta(id="r", name="n", created_at=1.0))
    sink.step("r", step(1, 10))
    assert json.loads(out.read_text())["steps"][0]["step"] == 1
    sink.finish("r", "stopped", {"verdict": "healthy"})
    doc = json.loads(out.read_text())
    assert doc["status"] == "stopped" and doc["run"]["finished_at"]
    assert Histogram.from_bytes(base64.b64decode(doc["steps"][0]["hist"])).count == 100
    assert mem.status == "stopped" and len(mem.steps) == 1
