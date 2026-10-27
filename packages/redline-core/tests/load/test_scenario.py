from __future__ import annotations

import json
import random
import sys

import pytest

from redline_core.load import Profile, ScenarioError, load_scenario, parse_scenario
from redline_core.load.profile import parse_duration, parse_profile
from redline_core.load.scenario import render_path

BASE = {"base_url": "http://127.0.0.1:8000", "requests": [{"path": "/"}]}


def test_minimal_scenario_gets_defaults():
    sc = parse_scenario(BASE)
    assert sc.name == "load test"
    assert sc.timeout_s == 10
    assert sc.connections == 64 and sc.max_inflight == 1000
    (req,) = sc.requests
    assert (req.method, req.path, req.weight, req.expect_status) == ("GET", "/", 1.0, None)
    assert req.name == "GET /"
    assert sc.profile == Profile(mode="constant", rps=10.0, duration_s=30.0)


def test_full_scenario_round_trips():
    data = {
        "name": "mix",
        "base_url": "https://shop.test/",
        "headers": {"Accept": "application/json"},
        "timeout_s": "5s",
        "workers": 3,
        "seed": 7,
        "requests": [
            {"name": "list", "path": "/orders/?p={random_int:1:3}", "weight": 3},
            {
                "method": "post",
                "path": "/cart/",
                "json": {"sku": "A"},
                "expect_status": [200, 201],
                "headers": {"X-Test": 1},
            },
            {"method": "PUT", "path": "/raw/", "body": "x=1", "weight": 0},
        ],
        "profile": {
            "mode": "steps",
            "start_rps": 10,
            "end_rps": 30,
            "step_rps": 10,
            "step_duration_s": "20s",
            "stop_on": {"error_rate": "5%", "p95_ms": 800},
        },
    }
    sc = parse_scenario(data)
    assert sc.base_url == "https://shop.test"
    assert sc.timeout_s == 5.0 and sc.workers == 3 and sc.seed == 7
    assert sc.requests[1].method == "POST" and sc.requests[1].has_json
    assert sc.requests[1].expect_status == (200, 201)
    assert sc.requests[1].headers == {"X-Test": "1"}
    assert sc.requests[2].body == "x=1" and sc.requests[2].weight == 0
    assert sc.profile.stop_on.error_rate == 0.05 and sc.profile.stop_on.p95_ms == 800
    again = parse_scenario(sc.to_dict())
    assert again.to_dict() == sc.to_dict()


@pytest.mark.parametrize(
    ("patch", "path", "fragment"),
    [
        ({"requests": []}, "requests", "non-empty"),
        ({"requests": [{"path": "x"}]}, "requests[0].path", "starting with '/'"),
        (
            {"requests": [{"path": "/", "methd": "GET"}]},
            "requests[0].methd",
            "did you mean 'method'",
        ),
        ({"requests": [{"path": "/", "method": "FETCH"}]}, "requests[0].method", "must be one of"),
        ({"requests": [{"path": "/", "weight": -1}]}, "requests[0].weight", "≥ 0"),
        ({"requests": [{"path": "/", "weight": 0}]}, "requests", "weight > 0"),
        (
            {"requests": [{"path": "/", "json": {}, "body": "x"}]},
            "requests[0]",
            "either json or body",
        ),
        (
            {"requests": [{"path": "/", "expect_status": 999}]},
            "requests[0].expect_status",
            "status",
        ),
        ({"requests": [{"path": "/orders/{id}"}]}, "requests[0].path", "unknown placeholder"),
        ({"base_url": "localhost:8000"}, "base_url", "http://"),
        ({"profile": {"mode": "spike"}}, "profile.mode", "must be one of"),
        ({"profile": {"mode": "ramp", "end_rps": 5}}, "profile.start_rps", "required"),
        ({"profile": {"rps": "fast"}}, "profile.rps", "requests per second"),
        ({"profile": {"rps": 5, "duration_s": "soon"}}, "profile.duration_s", "duration"),
        (
            {"profile": {"stop_on": {"p99_ms": 3}}},
            "profile.stop_on.p99_ms",
            "did you mean 'p95_ms'",
        ),
        (
            {"profile": {"stop_on": {"error_rate": 2}}},
            "profile.stop_on.error_rate",
            "between 0 and 1",
        ),
        (
            {"profile": {"mode": "steps", "start_rps": 10, "end_rps": 5}},
            "profile.end_rps",
            "≥ start_rps",
        ),
        ({"workers": 0}, "workers", "≥ 1"),
        ({"tiemout_s": 3}, "tiemout_s", "did you mean 'timeout_s'"),
        ({"headers": {"bad header": "x"}}, "headers", "invalid header name"),
    ],
)
def test_errors_name_the_bad_key(patch, path, fragment):
    with pytest.raises(ScenarioError) as info:
        parse_scenario({**BASE, **patch})
    assert info.value.path == path
    assert fragment in str(info.value)
    assert str(info.value).startswith(f"{path}: ")


def test_private_keys_are_kept_and_base_url_optional():
    sc = parse_scenario({"requests": [{"path": "/"}], "_crash_after_s": 1})
    assert sc.base_url is None
    assert sc.extra == {"_crash_after_s": 1}


def test_json_toml_and_python_files(tmp_path):
    (tmp_path / "s.json").write_text(json.dumps(BASE))
    (tmp_path / "s.py").write_text(f"SCENARIO = {BASE!r}\n")
    (tmp_path / "s.toml").write_text(
        'base_url = "http://h:1"\n\n[[requests]]\npath = "/a"\nweight = 2\n\n'
        '[profile]\nmode = "ramp"\nstart_rps = 1\nend_rps = 9\nduration_s = "10s"\n'
    )
    assert load_scenario(tmp_path / "s.json").name == "s"
    assert load_scenario(str(tmp_path / "s.py")).requests[0].path == "/"
    if sys.version_info >= (3, 11):
        sc = load_scenario(tmp_path / "s.toml")
        assert sc.profile.mode == "ramp" and sc.requests[0].weight == 2


def test_file_errors_name_the_file(tmp_path):
    with pytest.raises(ScenarioError, match="file not found"):
        load_scenario(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(ScenarioError, match=r"bad\.json: invalid JSON"):
        load_scenario(bad)
    (tmp_path / "x.yaml").write_text("a: 1")
    with pytest.raises(ScenarioError, match="unsupported scenario type"):
        load_scenario(tmp_path / "x.yaml")
    (tmp_path / "noscen.py").write_text("X = 1\n")
    with pytest.raises(ScenarioError, match="must define SCENARIO"):
        load_scenario(tmp_path / "noscen.py")
    (tmp_path / "w.json").write_text(json.dumps({**BASE, "workers": "many"}))
    with pytest.raises(ScenarioError) as info:
        load_scenario(tmp_path / "w.json")
    assert str(info.value).endswith("w.json: workers: must be an integer ≥ 1 (got 'many')")


def test_toml_on_310_without_tomli_is_a_clear_error(monkeypatch, tmp_path):
    from redline_core.load import scenario as mod

    monkeypatch.setattr(mod.sys, "version_info", (3, 10, 0))
    monkeypatch.setitem(sys.modules, "tomli", None)
    (tmp_path / "s.toml").write_text('base_url = "http://h"\n')
    with pytest.raises(ScenarioError, match="needs the 'tomli' package"):
        load_scenario(tmp_path / "s.toml")


def test_placeholders():
    rng = random.Random(1)
    out = {render_path("/o/{random_int:1:3}/", rng) for _ in range(200)}
    assert out == {"/o/1/", "/o/2/", "/o/3/"}
    assert render_path("/c/{random_choice:a|b}", rng) in ("/c/a", "/c/b")
    assert len(render_path("/u/{uuid}", rng)) == len("/u/") + 32
    assert render_path("/plain", rng) == "/plain"
    with pytest.raises(ValueError, match="B must be"):
        render_path("/{random_int:5:1}", rng)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(60, 60.0), ("60s", 60.0), ("1.5m", 90.0), ("500ms", 0.5), ("1h", 3600.0), ("2", 2.0)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["0s", -1, "x", True, None, "5d"])
def test_parse_duration_rejects(value):
    with pytest.raises(ScenarioError):
        parse_duration(value)


def test_profile_mode_is_inferred():
    assert parse_profile({"rps": 5}).mode == "constant"
    assert parse_profile({"start_rps": 1, "end_rps": 5}).mode == "ramp"
    assert parse_profile({"start_rps": 1, "end_rps": 5, "step_rps": 1}).mode == "steps"
    assert parse_profile({"mode": "ramp", "start_rps": 0, "end_rps": 4}).plan()[0].target_rps > 0
    with pytest.raises(ScenarioError) as info:
        parse_profile({"mode": "ramp", "start_rps": 0, "end_rps": 0})
    assert info.value.path == "profile.end_rps"
