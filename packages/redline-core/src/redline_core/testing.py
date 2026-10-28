"""Capture instrumented requests in the current execution context for assertions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .model import Trace

_capture: ContextVar[tuple[str, list[Trace]] | None] = ContextVar(
    "redline_test_capture", default=None
)


@contextmanager
def capture_traces(name: str = "") -> Iterator[list[Trace]]:
    traces: list[Trace] = []
    token = _capture.set((name, traces))
    try:
        yield traces
    finally:
        _capture.reset(token)


def active_capture() -> tuple[str, list[Trace]] | None:
    return _capture.get()


def assert_traces(
    traces: list[Trace],
    *,
    max_queries: int | None = None,
    max_external_calls: int | None = None,
    no_n_plus_one: bool = False,
    forbidden_issues: tuple[str, ...] = (),
) -> None:
    if not traces:
        raise AssertionError(
            "No instrumented requests were captured; check the adapter and ignore_paths."
        )
    forbidden = set(forbidden_issues)
    if no_n_plus_one:
        forbidden.add("n_plus_one")
    failures = []
    for trace in traces:
        label = f"{trace.method} {trace.route or trace.path}"
        if max_queries is not None and trace.query_count > max_queries:
            failures.append(f"{label}: {trace.query_count} queries exceeds {max_queries}")
        if max_external_calls is not None:
            count = sum(s.kind == "http_out" for s in trace.spans)
            if trace.truncated:
                failures.append(f"{label}: external-call count unavailable: trace truncated")
            elif count > max_external_calls:
                failures.append(f"{label}: {count} external calls exceeds {max_external_calls}")
        if forbidden and trace.truncated:
            failures.append(f"{label}: issue coverage incomplete: trace truncated")
        for issue in trace.issues:
            if issue.kind in forbidden:
                failures.append(f"{label}: {issue.title}")
    if failures:
        raise AssertionError("Redline performance assertions failed:\n" + "\n".join(failures))


@contextmanager
def assert_no_queries() -> Iterator[None]:
    """Assert a block inside an active instrumented request does not access SQL."""
    from .recorder import current

    builder = current()
    if builder is None or not builder.active:
        raise RuntimeError("assert_no_queries needs an active instrumented request")
    before = builder.query_count
    span_start = len(builder.spans)
    yield
    count = builder.query_count - before
    if count:
        spans = [s for s in builder.spans[span_start:] if s.kind == "sql"]
        site = spans[-1].callsite.location if spans and spans[-1].callsite else "unknown callsite"
        raise AssertionError(
            f"Unexpected SQL in no-query block: {count} queries; last access at {site}"
        )
