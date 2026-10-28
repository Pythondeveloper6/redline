"""pytest integration, imported only by pytest's plugin loader."""

from __future__ import annotations

import pytest

from .testing import assert_traces, capture_traces


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "redline(max_queries=None, max_external_calls=None, "
        "no_n_plus_one=False, forbidden_issues=()): assert instrumented request budgets",
    )


def _policy(marker):
    if marker.args:
        raise pytest.UsageError("redline marker accepts keyword arguments only")
    allowed = {"max_queries", "max_external_calls", "no_n_plus_one", "forbidden_issues"}
    if set(marker.kwargs) - allowed:
        raise pytest.UsageError("unknown redline marker option")
    policy = dict(marker.kwargs)
    for key in ("max_queries", "max_external_calls"):
        value = policy.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise pytest.UsageError(f"{key} must be a non-negative integer")
    if not isinstance(policy.get("no_n_plus_one", False), bool):
        raise pytest.UsageError("no_n_plus_one must be a boolean")
    kinds = policy.get("forbidden_issues", ())
    if not isinstance(kinds, (tuple, list)) or any(not isinstance(k, str) or not k for k in kinds):
        raise pytest.UsageError("forbidden_issues must be a list of issue kinds")
    return policy


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    marker = item.get_closest_marker("redline")
    if marker is None:
        yield
        return
    policy = _policy(marker)
    with capture_traces(item.nodeid) as traces:
        outcome = yield
    if outcome.excinfo is None:
        try:
            assert_traces(traces, **policy)
        except AssertionError as exc:
            outcome.force_exception(exc)


@pytest.fixture
def redline_capture():
    """For explicit blocks, use ``with redline_capture() as traces: ...``."""
    return capture_traces
