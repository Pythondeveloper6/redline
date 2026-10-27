"""Outbound-HTTP detectors, from ``http_out`` spans.

==================  ==================================================================
``slow_external``   a call takes ``>= slow_external_ms`` (*critical* at ``>= 5x``);
                    grouped by method + URL (without the query string) + callsite
``duplicate_external``  the same method + full URL is called ``>= 2`` times (*warning* at
                    ``>= 5`` copies or when together they exceed ``slow_external_ms``)
``retry_storm``     the same method + host is called ``>= 3`` times and at least one
                    attempt failed (5xx, 429 or no status at all); *critical* at ``>= 6``
==================  ==================================================================

A group that is already a retry storm is not reported again as a duplicate.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..conf import Settings
from ..model import Issue, Severity, Span, Trace
from ._common import issue_id, plural, where

__all__ = ["detect", "host_of"]

RETRY_MIN = 3


def host_of(span: Span) -> str:
    """The target host of an ``http_out`` span (from its URL, else its name)."""
    url = span.data.get("url")
    if isinstance(url, str):
        try:
            host = urlsplit(url).netloc.rsplit("@", 1)[-1]
        except ValueError:
            host = ""
        if host:
            return host
    parts = span.name.split(" ", 1)
    target = parts[-1]
    return target.split("/", 1)[0]


def _method(span: Span) -> str:
    return str(span.data.get("method") or span.name.split(" ", 1)[0]).upper()


def _url(span: Span) -> str:
    url = span.data.get("url")
    if isinstance(url, str) and url:
        return url
    return span.name.split(" ", 1)[-1]


def _no_query(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]


def _failed(span: Span) -> bool:
    status = span.data.get("status")
    return not isinstance(status, int) or status >= 500 or status == 429


def detect(trace: Trace, settings: Settings) -> list[Issue]:
    calls = [s for s in trace.spans if s.kind == "http_out"]
    if not calls:
        return []
    issues = _retry_storms(calls)
    stormed = {sid for issue in issues for sid in issue.span_ids}
    issues.extend(_slow(calls, settings))
    issues.extend(_duplicates(calls, settings, stormed))
    return issues


def _slow(calls: list[Span], settings: Settings) -> list[Issue]:
    limit = settings.slow_external_ms
    groups: dict[tuple[str, str, str | None], list[Span]] = {}
    for s in calls:
        if s.duration_ms >= limit:
            loc = s.callsite.location if s.callsite else None
            groups.setdefault((_method(s), _no_query(_url(s)), loc), []).append(s)
    issues = []
    for (method, url, loc), group in groups.items():
        worst = max(s.duration_ms for s in group)
        callsite = group[0].callsite
        host = host_of(group[0])
        n = len(group)
        severity: Severity = "critical" if worst >= 5 * limit else "warning"
        issues.append(
            Issue(
                id=issue_id("slow_external", f"{method} {url}", loc),
                kind="slow_external",
                severity=severity,
                title=f"Slow external call: {method} {host} took {worst:.0f} ms{where(callsite)}",
                detail=(
                    f"{'The request to' if n == 1 else f'{n} requests to'} {url} took "
                    f"{'' if n == 1 else 'up to '}{worst:.1f} ms, over the {limit:g} ms "
                    "budget, and the response waited for it. Set a timeout, cache the "
                    "answer, or move the call to a background task."
                ),
                span_ids=[s.id for s in group],
                fingerprint=f"{method} {url}",
                callsite=callsite,
                count=n,
                total_ms=round(sum(s.duration_ms for s in group), 3),
                extra={
                    "host": host,
                    "method": method,
                    "url": url,
                    "max_ms": round(worst, 3),
                    "threshold_ms": limit,
                },
            )
        )
    return issues


def _duplicates(calls: list[Span], settings: Settings, skip: set[str]) -> list[Issue]:
    groups: dict[tuple[str, str], list[Span]] = {}
    for s in calls:
        if s.id not in skip:
            groups.setdefault((_method(s), _url(s)), []).append(s)
    issues = []
    for (method, url), group in groups.items():
        n = len(group)
        if n < 2:
            continue
        total = sum(s.duration_ms for s in group)
        callsite = group[0].callsite
        loc = callsite.location if callsite else None
        host = host_of(group[0])
        severity: Severity = "warning" if n >= 5 or total >= settings.slow_external_ms else "info"
        issues.append(
            Issue(
                id=issue_id("duplicate_external", f"{method} {url}", loc),
                kind="duplicate_external",
                severity=severity,
                title=f"Duplicate external call ×{n}: {method} {host}{where(callsite)}",
                detail=(
                    f"{method} {url} was requested {plural(n, 'time')} in one request "
                    f"({total:.1f} ms in total). Make the call once and reuse the answer, "
                    "or cache it."
                ),
                span_ids=[s.id for s in group],
                fingerprint=f"{method} {url}",
                callsite=callsite,
                count=n,
                total_ms=round(total, 3),
                extra={"host": host, "method": method, "url": url},
            )
        )
    return issues


def _retry_storms(calls: list[Span]) -> list[Issue]:
    groups: dict[tuple[str, str], list[Span]] = {}
    for s in calls:
        groups.setdefault((_method(s), host_of(s)), []).append(s)
    issues = []
    for (method, host), group in groups.items():
        n = len(group)
        failures = [s for s in group if _failed(s)]
        if n < RETRY_MIN or not failures:
            continue
        total = sum(s.duration_ms for s in group)
        callsite = group[0].callsite
        loc = callsite.location if callsite else None
        statuses = sorted({str(s.data.get("status") or "none") for s in failures})
        severity: Severity = "critical" if n >= 2 * RETRY_MIN else "warning"
        issues.append(
            Issue(
                id=issue_id("retry_storm", f"{method} {host}", loc),
                kind="retry_storm",
                severity=severity,
                title=f"Retry storm: {n} × {method} {host}, {len(failures)} failed"
                + where(callsite),
                detail=(
                    f"{method} requests to {host} were made {plural(n, 'time')} "
                    f"({total:.1f} ms in total) and {len(failures)} of them failed "
                    f"(status {', '.join(statuses)}). Retrying inside a request multiplies "
                    "its latency; cap the attempts, back off, and fail fast."
                ),
                span_ids=[s.id for s in group],
                fingerprint=f"{method} {host}",
                callsite=callsite,
                count=n,
                total_ms=round(total, 3),
                extra={
                    "host": host,
                    "method": method,
                    "failed": len(failures),
                    "statuses": statuses,
                },
            )
        )
    return issues
