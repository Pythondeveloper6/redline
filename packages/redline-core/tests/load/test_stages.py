"""Stages profiles (spike, soak, custom shapes), recovery time and drift."""

from __future__ import annotations

import http.server
import threading
import time

import pytest

from redline_core import cli
from redline_core.histogram import Histogram
from redline_core.load import analysis, engine
from redline_core.load.analysis import StepResult, analyze, drift, find_knee, recovery
from redline_core.load.profile import ScenarioError, parse_profile
from redline_core.load.scenario import parse_scenario


def step(n, rps, p95, *, dur=10.0, errors=0, ok=100, partial=False):
    h = Histogram()
    for _ in range(max(1, ok)):
        h.record(p95)
    return StepResult(
        step=n, target_rps=rps, started_at=0.0, ended_at=dur, duration_s=dur,
        sent=ok + errors, ok=ok, errors=errors, hist=h, partial=partial,
    )  # fmt: skip


def seconds(step_no, start, values, errors=0):
    return [
        {"t": start + i + 1, "step": step_no, "sent": 10, "ok": 10 - errors, "errors": errors,
         "dropped": 0, "p50_ms": v, "p95_ms": v}
        for i, v in enumerate(values)
    ]  # fmt: skip


# ------------------------------------------------------------------ the profile
def test_stages_plan_and_describe():
    p = parse_profile(
        {"stages": [{"rps": 10, "duration_s": "30s"}, {"rps": 200, "duration_s": 20},
                    {"rps": 0, "duration_s": 5}, {"rps": 10, "duration_s": "1m"}]}
    )  # fmt: skip
    assert p.mode == "stages"
    plan = p.plan()
    assert [(x.index, x.target_rps, x.start_s, x.duration_s) for x in plan] == [
        (1, 10.0, 0.0, 30.0), (2, 200.0, 30.0, 20.0), (3, 0.0, 50.0, 5.0), (4, 10.0, 55.0, 60.0),
    ]  # fmt: skip
    assert p.max_rps == 200.0 and p.total_duration_s == 115.0 and p.step_count() == 4
    assert "200 rps × 20s" in p.describe()
    assert parse_profile(p.to_dict()).stages == p.stages  # round trips (and via a scenario)


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "stages"},
        {"stages": []},
        {"stages": [{"rps": 10}]},
        {"stages": [{"rps": -1, "duration_s": 5}]},
        {"stages": [{"rps": 0, "duration_s": 5}]},  # never sends anything
        {"stages": [{"rps": 5, "duration_s": 5, "x": 1}]},
        {"stages": "10:5"},
    ],
)
def test_invalid_stages(bad):
    with pytest.raises(ScenarioError):
        parse_profile(bad)


def test_a_scenario_file_can_hold_stages():
    sc = parse_scenario(
        {"name": "s", "base_url": "http://x", "requests": [{"path": "/a"}],
         "profile": {"stages": [{"rps": 5, "duration_s": 5}, {"rps": 50, "duration_s": 5}]}}
    )  # fmt: skip
    assert sc.profile.mode == "stages" and sc.to_dict()["profile"]["stages"][1]["rps"] == 50.0


# ------------------------------------------------------------------ knee
def test_the_knee_ignores_steps_where_the_rate_falls():
    spike = [step(1, 10, 20), step(2, 200, 22, ok=2000), step(3, 10, 900, errors=40)]
    knee, _ = find_knee(spike)
    assert knee is None  # step 3 is slow and erroring but the rate fell: recovery, not a knee
    rising = [step(1, 10, 20), step(2, 200, 900)]
    assert find_knee(rising)[0].step == 2  # a rising step still decides it


# ------------------------------------------------------------------ recovery
def test_recovery_time_after_a_spike():
    steps = [step(1, 10, 20), step(2, 200, 300), step(3, 10, 90)]
    secs = seconds(3, 20, [400, 300, 200, 90, 60, 30, 22, 21, 20, 20])
    out = recovery(steps, secs, {3: 20.0})
    assert len(out) == 1
    r = out[0]
    assert r["step"] == 3 and r["baseline_step"] == 1
    assert r["baseline_p95_ms"] == pytest.approx(20, rel=0.05) and r["peak_p95_ms"] >= 280
    # within 25% of 20 ms (<= 25 ms) for 3 seconds running starts at the 7th second
    assert r["recovered_after_s"] == 7.0


def test_a_run_that_never_recovers_says_so():
    steps = [step(1, 10, 20), step(2, 200, 300), step(3, 10, 400)]
    out = recovery(steps, seconds(3, 20, [400] * 8), {3: 20.0})
    assert out[0]["recovered_after_s"] is None and out[0]["seconds_observed"] == 8


def test_no_recovery_entry_without_a_fall_or_a_baseline():
    assert recovery([step(1, 10, 20), step(2, 20, 25)], seconds(2, 0, [20] * 5), {}) == []
    assert recovery([step(1, 50, 20), step(2, 10, 25)], seconds(2, 0, [20] * 5), {}) == []


# ------------------------------------------------------------------ drift
def test_a_leaking_step_is_degrading_and_a_flat_one_is_stable(monkeypatch):
    long_step = step(1, 20, 50, dur=120.0)
    leak = seconds(1, 0, [20 + i * 2 for i in range(60)])
    flat = seconds(1, 0, [20 + (i % 3) for i in range(60)])
    d = drift([long_step], leak)[0]
    assert d["verdict"] == "degrading" and d["p95_slope_ms_per_min"] > 50
    assert d["last_third_p95_ms"] > 2 * d["first_third_p95_ms"]
    assert drift([long_step], flat)[0]["verdict"] == "stable"
    err = [dict(s, errors=min(8, i // 5), ok=10 - min(8, i // 5)) for i, s in enumerate(flat)]
    assert drift([long_step], err)[0]["verdict"] == "degrading"  # error rate climbing
    assert drift([step(1, 20, 50, dur=10.0)], leak) == []  # too short to judge
    assert drift([long_step], leak[:5]) == []  # too few samples


def test_analyze_adds_recovery_and_drift_only_when_there_is_something():
    plain = analyze([step(1, 10, 20), step(2, 20, 25)])
    assert "recovery" not in plain and "drift" not in plain
    steps = [step(1, 10, 20), step(2, 200, 22, ok=2000), step(3, 10, 90)]
    out = analyze(steps, seconds(3, 20, [30, 22, 21, 20, 20]), {3: 20.0})
    assert out["recovery"][0]["recovered_after_s"] == 2.0
    assert out["knee_step"] is None and out["verdict"] == "healthy"


# ------------------------------------------------------------------ end to end
def _server(handler_factory):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_a_spike_run_reports_recovery(http_server):
    _, url = http_server
    sc = parse_scenario({"name": "spike", "base_url": url, "requests": [{"path": "/a"}]})
    result = engine.run(
        sc,
        profile={"stages": [{"rps": 10, "duration_s": 3}, {"rps": 60, "duration_s": 3},
                            {"rps": 10, "duration_s": 5}]},
        workers=2,
    )  # fmt: skip
    assert result.status == "done"
    assert [s.step for s in result.steps] == [1, 2, 3]
    # The real server and load generator share the CI runner, so measured latency can
    # legitimately identify a knee here. Analyzer behavior is covered with fixed samples above.
    assert len(result.summary["recovery"]) == 1
    assert result.summary["recovery"][0]["step"] == 3


def test_a_leaking_app_is_reported_as_degrading(monkeypatch):
    monkeypatch.setattr(analysis, "DRIFT_MIN_STEP_S", 8.0)
    monkeypatch.setattr(analysis, "DRIFT_MIN_SAMPLES", 6)
    counter = {"n": 0}
    lock = threading.Lock()

    class Leaky(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            with lock:
                counter["n"] += 1
                n = counter["n"]
            time.sleep(min(0.25, 0.0006 * n))  # every request is slower than the last
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server, url = _server(Leaky)
    try:
        sc = parse_scenario({"name": "leak", "base_url": url, "requests": [{"path": "/a"}]})
        result = engine.run(
            sc, profile={"mode": "constant", "rps": 30, "duration_s": 14}, workers=2
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.summary["drift_verdict"] == "degrading"
    assert result.summary["drift"][0]["p95_slope_ms_per_min"] > 0


# ------------------------------------------------------------------ CLI
def test_cli_stages_flag(tmp_path, capsys, http_server):
    import json

    _, url = http_server
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"name": "c", "base_url": url, "requests": [{"path": "/a"}]}))
    code = cli.main(["load", str(path), "--no-store", "--stages", "5:2s,30:2s,5:4s", "-q"])
    out = capsys.readouterr().out
    assert code == 0 and "recovery: step 3" in out


@pytest.mark.parametrize("value", ["10", "10:", "x:5s", "10:5s,20"])
def test_cli_stages_flag_validates(tmp_path, capsys, value):
    import json

    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"name": "c", "base_url": "http://127.0.0.1:1", "requests": [{"path": "/a"}]})
    )
    assert cli.main(["load", str(path), "--no-store", "--stages", value]) == 2
    assert "RPS:DURATION" in capsys.readouterr().err


def test_load_init_writes_a_stages_profile(tmp_path):
    import json

    spec = tmp_path / "o.json"
    spec.write_text(
        json.dumps({"openapi": "3.0.0", "info": {"title": "x"}, "paths": {"/a": {"get": {}}}})
    )
    out = tmp_path / "sc.toml"
    args = [
        "load",
        "init",
        "--from-openapi",
        str(spec),
        "-o",
        str(out),
        "--stages",
        "5:10s,50:10s,5:20s",
    ]
    assert cli.main(args) == 0
    text = out.read_text()
    assert "stages = [" in text and "200" not in text
    from redline_core.load.scenario import load_scenario

    sc = load_scenario(str(out))
    assert sc.profile.mode == "stages" and len(sc.profile.stages) == 3
