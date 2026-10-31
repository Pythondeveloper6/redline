"""pytest integration, imported only by pytest's plugin loader."""

from __future__ import annotations

import pytest

from .testing import assert_traces, capture_traces

_OBSERVED = pytest.StashKey[dict]()
_RECORDED = pytest.StashKey[dict]()


def pytest_addoption(parser):
    parser.getgroup("redline").addoption(
        "--redline-all",
        action="store_true",
        default=False,
        help="apply SQL-error assertions to every test; tests must capture instrumented work",
    )

    group = parser.getgroup("redline")
    group.addoption(
        "--redline-ratchet",
        metavar="FILE",
        default=None,
        help="committed JSON of per-test query counts: more queries than recorded fails the test",
    )
    group.addoption(
        "--redline-ratchet-update",
        action="store_true",
        default=False,
        help="after a fully passing session, write fewer/new query counts to --redline-ratchet",
    )


def pytest_configure(config):
    config.stash[_OBSERVED] = {}
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
            _ratchet_test(item, traces)
        except AssertionError as exc:
            outcome.force_exception(exc)


def _ratchet_path(config) -> str | None:
    return config.getoption("--redline-ratchet")


def _ratchet_test(item, traces) -> None:
    """Compare the test's busiest request with the recorded count (and note it)."""
    path = _ratchet_path(item.config)
    if not path or not traces:
        return
    from .check import ratchet

    queries = max(t.query_count for t in traces)
    item.config.stash[_OBSERVED][item.nodeid] = {"queries": queries}
    recorded = item.config.stash.get(_RECORDED, None)
    if recorded is None:
        try:
            recorded = ratchet.load(path, "tests")
        except FileNotFoundError:
            recorded = {}
        except ratchet.RatchetError as exc:
            raise pytest.UsageError(str(exc)) from None
        item.config.stash[_RECORDED] = recorded
    old = recorded.get(item.nodeid, {}).get("queries")
    if old is not None and queries > old:
        raise AssertionError(
            f"Redline ratchet: {queries} queries, but {old} are recorded in {path}. "
            "Fix the regression (the file only ever tightens)."
        )


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    path = _ratchet_path(config)
    observed = config.stash.get(_OBSERVED, {})
    if not path or not config.getoption("--redline-ratchet-update") or not observed:
        return
    if exitstatus != 0:  # only a fully green session may move the ratchet
        return
    from .check import ratchet

    recorded = config.stash.get(_RECORDED, {}) or {}
    result = ratchet.compare(recorded, observed, latency_pct=0, latency_ms=0)
    if result.ok and result.updated != recorded:
        ratchet.write(path, result.updated, "tests")


@pytest.fixture
def redline_capture():
    """For explicit blocks, use ``with redline_capture() as traces: ...``."""
    return capture_traces
