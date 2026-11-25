"""The recording probe: one tagged request before the run, then check it was recorded."""

from __future__ import annotations

import pytest

from redline_core.load import engine
from redline_core.load.probe import ProbeFailed, run_probe
from redline_core.load.scenario import parse_scenario


def scenario(url, **extra):
    return parse_scenario(
        {
            "name": "p",
            "base_url": url,
            "headers": {"X-Api": "k"},
            "requests": [
                {"method": "POST", "path": "/write", "weight": 5},
                {"method": "GET", "path": "/a/{random_int:1:3}"},
            ],
            **extra,
        }
    )


def test_probe_sends_one_safe_tagged_request_and_finds_the_trace(http_server):
    state, url = http_server
    seen = []
    run_probe(scenario(url), "run1", lambda rid: seen.append(rid) or True, timeout_s=2.0)
    assert state.count == 1
    assert state.headers == ["run1;step=probe"]
    assert state.paths[0].startswith("/a/")  # never the POST
    assert seen == ["run1"]


def test_probe_fails_with_the_likely_causes_when_nothing_is_recorded(http_server):
    _, url = http_server
    with pytest.raises(ProbeFailed) as info:
        run_probe(scenario(url), "run2", lambda rid: False, timeout_s=0.5)
    text = str(info.value)
    assert "not recording this run" in text
    assert "different store" in text and "sampling" in text and "recording disabled" in text


def test_probe_reports_an_unreachable_application():
    with pytest.raises(ProbeFailed) as info:
        run_probe(scenario("http://127.0.0.1:1"), "run3", lambda rid: True, timeout_s=0.5)
    assert "cannot reach" in str(info.value)


def test_probe_is_skipped_when_the_scenario_has_no_get(http_server):
    state, url = http_server
    sc = parse_scenario(
        {"name": "w", "base_url": url, "requests": [{"method": "POST", "path": "/w"}]}
    )
    assert run_probe(sc, "run4", lambda rid: False, timeout_s=0.2) is False
    assert state.count == 0


def test_engine_stops_a_failed_run_before_any_load(http_server):
    state, url = http_server

    def probe():
        raise ProbeFailed("the application is not recording this run")

    result = engine.run(
        scenario(url),
        profile={"mode": "constant", "rps": 5, "duration_s": 5},
        probe=probe,
        run_id="r5",
    )
    assert result.status == "failed"
    assert "not recording" in result.message
    assert state.count == 0
