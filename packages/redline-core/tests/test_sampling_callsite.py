from __future__ import annotations

import os
import sys

from redline_core.callsite import MAX_STACK, CallsiteCapturer
from redline_core.conf import Settings
from redline_core.sampling import Sampler

HERE = os.path.dirname(os.path.abspath(__file__))


def test_ignored_prefix_regex_and_disabled():
    s = Sampler(Settings(ignore_paths=("/static/", r"^/api/v\d+/health$")))
    assert s.ignored("/static/x.css")
    assert s.ignored("/api/v2/health")
    assert not s.ignored("/api/v2/healthz")
    assert Sampler(Settings(enabled=False)).ignored("/anything")


def test_roll_and_keep():
    s = Sampler(Settings(sample_rate=0.5, always_keep_slower_than_ms=100), rng=lambda: 0.4)
    assert s.roll() is True
    s2 = Sampler(Settings(sample_rate=0.5), rng=lambda: 0.6)
    assert s2.roll() is False
    assert s.keep(rolled=True, status=200, duration_ms=1) == "rate"
    assert Sampler(Settings()).keep(rolled=True, status=200, duration_ms=1) == "all"
    assert s.keep(rolled=False, status=503, duration_ms=1) == "error"
    assert s.keep(rolled=False, status=200, duration_ms=1, error=True) == "error"
    assert s.keep(rolled=False, status=200, duration_ms=100) == "slow"
    assert s.keep(rolled=False, status=200, duration_ms=1, profiled=True) == "profile"
    assert s.keep(rolled=False, status=404, duration_ms=1) is None
    no_err = Sampler(Settings(sample_rate=0, always_keep_errors=False))
    assert no_err.keep(rolled=False, status=500, duration_ms=1) is None
    assert Sampler(Settings(sample_rate=0.0)).roll() is False


def test_wants_profile_modes():
    assert not Sampler(Settings(profile="off")).wants_profile(True)
    assert Sampler(Settings(profile="all")).wants_profile(False)
    assert Sampler(Settings(profile="opt_in")).wants_profile(True)
    assert not Sampler(Settings(profile="opt_in")).wants_profile(False)
    sampled = Sampler(Settings(profile="sampled", profile_sample_rate=0.1), rng=lambda: 0.05)
    assert sampled.wants_profile(False)
    assert not Sampler(
        Settings(profile="sampled", profile_sample_rate=0.1), rng=lambda: 0.5
    ).wants_profile(False)


def test_callsite_project_frames_only():
    cap = CallsiteCapturer(HERE)

    def inner():
        return cap.capture()

    def outer():
        return inner()

    cs = outer()
    assert cs.file == "test_sampling_callsite.py"
    assert cs.function == "inner"
    assert cs.code == "return cap.capture()"
    assert [f.function for f in cs.stack][-3:] == [
        "test_callsite_project_frames_only",
        "outer",
        "inner",
    ]
    # pytest/pluggy frames live in site-packages and are skipped
    assert all("site-packages" not in f.file for f in cs.stack)


def test_callsite_excludes_stdlib_redline_and_unknown():
    cap = CallsiteCapturer(HERE)
    assert cap.classify(os.__file__) is None
    import redline_core.recorder as r

    assert cap.classify(r.__file__) is None
    assert cap.classify("<frozen importlib._bootstrap>") is None
    assert cap.classify("/elsewhere/app.py") is None
    assert cap.classify(os.path.join(HERE, "pkg", "mod.py")) == "pkg/mod.py"
    assert (
        CallsiteCapturer(HERE, exclude=["/pkg/"]).classify(os.path.join(HERE, "pkg", "m.py"))
        is None
    )


def test_callsite_none_outside_project():
    assert CallsiteCapturer("/nonexistent").capture() is None


def test_callsite_stack_is_capped():
    cap = CallsiteCapturer(HERE)

    def rec(n):
        return cap.capture() if n == 0 else rec(n - 1)

    cs = rec(30)
    assert len(cs.stack) == MAX_STACK
    assert cs.stack[-1].line == cs.line


def test_callsite_cache_is_used():
    cap = CallsiteCapturer(HERE)
    cap.capture()
    assert os.path.abspath(__file__) in cap._cache or __file__ in cap._cache
    cap.clear_cache()
    assert cap._cache == {}
    assert sys._getframe() is not None
