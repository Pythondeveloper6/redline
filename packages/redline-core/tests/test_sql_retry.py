from __future__ import annotations

from redline_core import Settings
from redline_core.detectors import sql_failures
from redline_core.model import Span, Trace


def _trace(error_category, attempts, *, fingerprint="fp", route="/pay"):
    spans = []
    for index in range(attempts):
        span = Span(
            id=f"s{index}",
            parent_id=None,
            kind="sql",
            name="SELECT order",
            start_ms=float(index),
            duration_ms=1.0,
            data={
                "sql": "SELECT * FROM shop_order WHERE id = ?",
                "fingerprint": fingerprint,
                "db_alias": "default",
                "vendor": "mssql",
            },
        )
        if index == 0:
            span.data["error"] = {
                "type": "OperationalError",
                "category": error_category,
                "sqlstate": "40001",
                "native": 1205,
            }
        spans.append(span)
    return Trace(
        id="trace",
        framework="other",
        method="GET",
        path=route,
        route=route,
        spans=spans,
    )


def _storms(trace, **settings):
    issues = sql_failures.detect(trace, Settings(**settings))
    return [issue for issue in issues if issue.kind == "sql_retry_storm"]


def test_retry_storm_after_a_transient_failure():
    issues = _storms(_trace("deadlock", 4), sql_retry_storm_threshold=3)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "warning"
    assert issue.count == 4
    assert issue.extra["retries"] == 3
    assert issue.extra["categories"] == ["deadlock"]
    assert "deadlock" in issue.detail


def test_retry_storm_is_critical_at_twice_the_threshold():
    issues = _storms(_trace("timeout", 7), sql_retry_storm_threshold=3)
    assert issues and issues[0].severity == "critical"


def test_non_transient_or_insufficient_retries_are_not_storms():
    assert not _storms(_trace("schema", 10), sql_retry_storm_threshold=3)
    assert not _storms(_trace("deadlock", 3), sql_retry_storm_threshold=3)
    assert not _storms(_trace("deadlock", 2), sql_retry_storm_threshold=3)


def test_retry_storm_groups_by_fingerprint():
    first = _trace("deadlock", 4, fingerprint="one", route="/a")
    second = _trace("deadlock", 4, fingerprint="two", route="/b")
    trace = Trace(
        id="combined",
        framework="other",
        method="GET",
        path="/combined",
        route="/combined",
        spans=[*first.spans, *second.spans],
    )
    issues = _storms(trace, sql_retry_storm_threshold=3)
    assert len(issues) == 2
