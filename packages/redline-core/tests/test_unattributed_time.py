"""``unattributed_time``: request time that no span accounts for."""

from __future__ import annotations

import os
import time

import pytest

from redline_core import Settings, SettingsError, instrument_client_method
from redline_core.conf import Settings as CoreSettings
from redline_core.coverage import gaps, uncovered_ms
from redline_core.detect import Detector
from redline_core.finding_explain import explain_issue
from redline_core.model import ISSUE_KINDS, Span, Trace, new_id
from redline_core.recorder import Recorder

ROOT = os.path.dirname(os.path.dirname(__file__))


def span(kind, start, duration):
    return Span(new_id(), None, kind, kind, start, duration, None, {})


def trace(duration, spans=(), *, framework="fastapi", method="GET", **kw):
    return Trace(
        id=new_id(),
        framework=framework,
        method=method,
        path="/reports",
        route="/reports",
        status=200,
        started_at=time.time(),
        duration_ms=duration,
        spans=list(spans),
        **kw,
    )


def found(t, **settings):
    s = Settings(project_root=ROOT, **settings)
    return [i for i in Detector(s).detect(t) if i.kind == "unattributed_time"]


# coverage -----------------------------------------------------------------------------------
def test_coverage_merges_overlap_and_ignores_wrappers():
    spans = [
        ("view", 0, 500),  # a wrapper covers nothing
        ("middleware", 0, 500),
        ("transaction", 0, 500),
        ("sql", 10, 100),
        ("custom", 50, 100),  # overlaps the sql span: 10..150 once
        ("http_out", 400, 50),
    ]
    assert gaps(spans, 500) == [(0, 10), (150, 400), (450, 500)]
    assert uncovered_ms(spans, 500) == pytest.approx(310)
    assert uncovered_ms([("sql", -20, 1000)], 100) == 0  # clipped to the request
    assert uncovered_ms([], 0) == 0


# detector ------------------------------------------------------------------------------------
def test_kind_is_registered_and_explained():
    assert "unattributed_time" in ISSUE_KINDS
    why = explain_issue("unattributed_time", {"threshold_ms": 100}, 1)
    assert "no recorded span" in why["rule"] and "100" in why["threshold"]


def test_fires_when_most_of_the_request_is_uncovered():
    t = trace(400, [span("sql", 0, 20), span("view", 0, 400)])
    (issue,) = found(t)
    assert issue.kind == "unattributed_time"
    assert issue.total_ms == pytest.approx(380)
    assert issue.extra["uncovered_ratio"] == pytest.approx(0.95)
    assert issue.severity == "warning"
    assert "not attributed" in issue.title and "instrument_client_method" in issue.detail
    assert issue.suggestion is not None


def test_info_severity_just_over_the_floor():
    (issue,) = found(trace(200, [span("sql", 0, 50)]))
    assert issue.severity == "info"


@pytest.mark.parametrize(
    "t",
    [
        trace(400, [span("sql", 0, 300)]),  # 25% uncovered: below the ratio
        trace(90, []),  # request shorter than the minimum
    ],
)
def test_quiet_below_ratio_or_minimum(t):
    assert found(t) == []


def test_quiet_when_uncovered_time_is_under_the_minimum_even_at_high_ratio():
    assert found(trace(150, [span("sql", 0, 40)]), unattributed_time_ms=200) == []


def test_ratio_zero_disables_and_thresholds_are_overridable():
    t = trace(400, [span("sql", 0, 250)])  # 37% uncovered
    assert found(t) == []
    assert len(found(t, unattributed_time_ratio=0.3)) == 1
    assert found(trace(400, []), unattributed_time_ratio=0) == []


def test_jobs_and_non_adapter_recordings_are_skipped():
    assert found(trace(400, [], framework="other")) == []
    assert found(trace(400, [], method="TASK")) == []


def test_silent_when_spans_were_dropped():
    t = trace(400, [span("sql", 0, 5)], truncated={"dropped_spans": 3})
    assert found(t) == []


def test_issue_id_is_stable_per_route():
    a, b = found(trace(400)), found(trace(900))
    assert a[0].id == b[0].id


# profile callsite ----------------------------------------------------------------------------
def _frames():
    return [
        {"name": "handler", "file": os.path.join(ROOT, "app", "views.py"), "line": 12},
        {"name": "sleep", "file": "/usr/lib/python3/time.py", "line": 1},
        {"name": "other_work", "file": os.path.join(ROOT, "app", "helpers.py"), "line": 7},
        {"name": "run", "file": "/x/site-packages/starlette/routing.py", "line": 3},
    ]


def test_evented_profile_names_the_project_frame_in_the_gap():
    profile = {
        "shared": {"frames": _frames()},
        "profiles": [
            {
                "type": "evented",
                "unit": "seconds",
                "startValue": 0,
                "events": [
                    {"type": "O", "at": 0.0, "frame": 3},
                    {"type": "O", "at": 0.0, "frame": 2},  # covered by the sql span below
                    {"type": "C", "at": 0.1, "frame": 2},
                    {"type": "O", "at": 0.1, "frame": 0},
                    {"type": "O", "at": 0.1, "frame": 1},  # the stdlib sleep inside handler
                    {"type": "C", "at": 0.4, "frame": 1},
                    {"type": "C", "at": 0.4, "frame": 0},
                    {"type": "C", "at": 0.4, "frame": 3},
                ],
            }
        ],
    }
    t = trace(400, [span("sql", 0, 100)], profile=profile)
    (issue,) = found(t)
    assert issue.callsite is not None
    assert (issue.callsite.file, issue.callsite.line, issue.callsite.function) == (
        "app/views.py",
        12,
        "handler",
    )
    assert "handler" in issue.detail and issue.extra["profiled"] is True


def test_sampled_profile_and_unusable_profiles():
    sampled = {
        "shared": {"frames": _frames()},
        "profiles": [
            {
                "type": "sampled",
                "unit": "milliseconds",
                "startValue": 0,
                "samples": [[3, 2], [3, 0, 1]],
                "weights": [100, 300],
            }
        ],
    }
    (issue,) = found(trace(400, [span("sql", 0, 100)], profile=sampled))
    assert issue.callsite.function == "handler"
    for bad in ({}, {"shared": {"frames": _frames()}, "profiles": [{"type": "?", "unit": "s"}]}):
        (issue,) = found(trace(400, [], profile=bad))
        assert issue.callsite is None and issue.extra["profiled"] is False


# end to end: the acceptance scenario -------------------------------------------------------
class UninstrumentedClient:
    def fetch(self, table):
        time.sleep(0.2)
        return table


def _request(recorder):
    builder = recorder.start_trace(framework="fastapi", method="GET", path="/reports")
    UninstrumentedClient().fetch("orders")
    return recorder.finish_trace(builder, status=200)


def test_sleep_in_an_uninstrumented_client_is_found_then_named_after_wrapping():
    recorder = Recorder(Settings.from_mapping({"project_root": ROOT, "sample_rate": 1.0}))
    before = _request(recorder)
    assert [i.kind for i in before.issues] == ["unattributed_time"]
    assert before.issues[0].total_ms >= 150

    restore = instrument_client_method(
        UninstrumentedClient, "fetch", client="acme", target=lambda self, a, k: a[0]
    )
    try:
        after = _request(recorder)
    finally:
        restore()
    assert [s.name for s in after.spans] == ["acme fetch orders"]
    assert after.spans[0].duration_ms >= 150
    assert [i for i in after.issues if i.kind == "unattributed_time"] == []


# settings ------------------------------------------------------------------------------------
def test_settings_defaults_validation_and_mapping():
    s = CoreSettings()
    assert (s.unattributed_time_ratio, s.unattributed_time_ms) == (0.5, 100.0)
    assert CoreSettings.from_mapping({"unattributed_time_ratio": 0.8}).unattributed_time_ratio
    for bad in ({"unattributed_time_ratio": 1.5}, {"unattributed_time_ms": -1}):
        with pytest.raises(SettingsError):
            CoreSettings(**bad)
    with pytest.raises(SettingsError):
        CoreSettings(unattributed_time_ms=float("inf"))
