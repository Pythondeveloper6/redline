"""pytest integration, imported only by pytest's plugin loader."""

from __future__ import annotations

import pytest

from .testing import assert_traces, capture_traces


def pytest_addoption(parser):
    parser.getgroup("redline").addoption(
        "--redline-all",
        action="store_true",
        default=False,
        help="apply SQL-error assertions to every test; tests must capture instrumented work",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "redline(max_queries=None, max_external_calls=None, "
        "no_n_plus_one=False, forbidden_issues=()): assert instrumented request budgets",
    )


def _policy(marker):
    if marker.args:
        raise pytest.UsageError("redline marker accepts keyword arguments only")
    allowed = {
        "max_queries",
        "max_external_calls",
        "no_n_plus_one",
        "forbidden_issues",
        "no_sql_errors",
        "no_session_leaks",
        "max_round_trips",
    }
    if set(marker.kwargs) - allowed:
        raise pytest.UsageError("unknown redline marker option")
    policy = {"no_sql_errors": True, **marker.kwargs}
    for key in ("max_queries", "max_external_calls", "max_round_trips"):
        value = policy.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise pytest.UsageError(f"{key} must be a non-negative integer")
    for key in ("no_n_plus_one", "no_sql_errors", "no_session_leaks"):
        if not isinstance(policy.get(key, False), bool):
            raise pytest.UsageError(f"{key} must be a boolean")
    kinds = policy.get("forbidden_issues", ())
    if not isinstance(kinds, (tuple, list)) or any(not isinstance(k, str) or not k for k in kinds):
        raise pytest.UsageError("forbidden_issues must be a list of issue kinds")
    return policy


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    marker = item.get_closest_marker("redline")
    if marker is None and not item.config.getoption("--redline-all"):
        yield
        return
    policy = _policy(marker) if marker is not None else {"no_sql_errors": True}
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
