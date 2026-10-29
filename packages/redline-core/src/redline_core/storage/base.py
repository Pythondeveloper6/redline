"""The :class:`Store` protocol and the value types it returns."""

from __future__ import annotations

import base64
import binascii
import builtins
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..model import Callsite, Suggestion, Trace

if TYPE_CHECKING:
    from ..runs import Run, RunStep

__all__ = [
    "WINDOWS",
    "Bottlenecks",
    "CacheStats",
    "EndpointStats",
    "ExtendedStore",
    "ExternalStats",
    "FilterError",
    "Filters",
    "History",
    "IssueGroup",
    "IssueIndexEntry",
    "Page",
    "Period",
    "PeriodComparison",
    "PeriodStats",
    "ReleaseInfo",
    "SqlStats",
    "Store",
    "decode_cursor",
    "encode_cursor",
    "parse_period",
    "parse_window",
    "percentile",
]

#: The windows the API accepts, in seconds.
WINDOWS: dict[str, float] = {"1h": 3_600.0, "24h": 86_400.0, "7d": 604_800.0}
MAX_BUCKETS = 48


class FilterError(ValueError):
    """A list filter or cursor could not be parsed (→ HTTP 400)."""


def parse_window(value: str | None, default: str = "24h") -> float:
    """``"1h"``/``"24h"``/``"7d"`` (or any ``<n>m|h|d``) → seconds."""
    text = (value or default).strip().lower()
    if text in WINDOWS:
        return WINDOWS[text]
    m = re.fullmatch(r"(\d+)([mhd])", text)
    if not m:
        raise FilterError(f"Invalid window {value!r}; use 1h, 24h or 7d.")
    n = int(m.group(1))
    if n <= 0:
        raise FilterError(f"Invalid window {value!r}.")
    return float(n) * {"m": 60, "h": 3_600, "d": 86_400}[m.group(2)]


def percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile of an already sorted sequence (0 for empty)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (len(sorted_values) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo))


def encode_cursor(started_at: float, trace_id: str) -> str:
    raw = f"{started_at!r}|{trace_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        started, _, trace_id = base64.urlsafe_b64decode(padded).decode("utf-8").partition("|")
        if not trace_id:
            raise ValueError
        return float(started), trace_id
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise FilterError("Invalid cursor.") from None


@dataclass
class Filters:
    """Trace list filters – exactly the query parameters of ``GET api/traces``."""

    q: str | None = None
    method: str | None = None
    status: str | None = None  # "2xx" | "3xx" | "4xx" | "5xx" | "<int>"
    min_ms: float | None = None
    min_queries: int | None = None
    has_issues: bool = False
    route: str | None = None
    tag: tuple[str, str] | None = None
    #: Substring of a query's normalised SQL (literals are ``?``), case-insensitive.
    sql: str | None = None
    #: Only traces that started strictly after this epoch.
    since: float | None = None
    #: Only replays of this trace (the ``replay_of`` tag).
    replay_of: str | None = None

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> Filters:
        """Parse raw query parameters (strings); raises :class:`FilterError`."""

        def text(name: str) -> str | None:
            value = params.get(name)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else None
            if value is None:
                return None
            value = str(value).strip()
            return value or None

        f = cls(q=text("q"), route=text("route"), sql=text("sql"), replay_of=text("replay_of"))
        method = text("method")
        f.method = method.upper() if method else None
        status = text("status")
        if status is not None:
            s = status.lower()
            if not (re.fullmatch(r"[1-5]xx", s) or re.fullmatch(r"\d{3}", s)):
                raise FilterError("status must be 2xx, 3xx, 4xx, 5xx or a status code.")
            f.status = s
        min_ms = text("min_ms")
        if min_ms is not None:
            try:
                f.min_ms = float(min_ms)
            except ValueError:
                raise FilterError("min_ms must be a number.") from None
        min_q = text("min_queries")
        if min_q is not None:
            try:
                f.min_queries = int(min_q)
            except ValueError:
                raise FilterError("min_queries must be an integer.") from None
        has = text("has_issues")
        f.has_issues = has is not None and has.lower() in ("1", "true", "yes", "on")
        tag = text("tag")
        if tag is not None:
            key, sep, value = tag.partition(":")
            if not sep or not key:
                raise FilterError("tag must look like key:value.")
            f.tag = (key, value)
        since = text("since")
        if since is not None:
            try:
                f.since = float(since)
            except ValueError:
                raise FilterError("since must be an epoch timestamp in seconds.") from None
        return f


@dataclass
class Page:
    """One page of trace summaries."""

    items: list[dict[str, Any]]
    next_cursor: str | None
    total: int

    def to_dict(self) -> dict[str, Any]:
        return {"items": self.items, "next_cursor": self.next_cursor, "total": self.total}


@dataclass
class EndpointStats:
    route: str
    method: str
    count: int
    error_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    avg_queries: float
    max_queries: int
    avg_query_ms: float
    issue_count: int
    series: list[dict[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "method": self.method,
            "count": self.count,
            "error_rate": self.error_rate,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "avg_queries": self.avg_queries,
            "max_queries": self.max_queries,
            "avg_query_ms": self.avg_query_ms,
            "issue_count": self.issue_count,
            "series": self.series,
        }


@dataclass
class IssueGroup:
    """One issue deduplicated across traces by its stable id."""

    id: str
    kind: str
    severity: str
    title: str
    detail: str
    occurrences: int
    first_seen: float
    last_seen: float
    routes: list[str]
    sample_trace_id: str
    sample_span_ids: list[str]
    suggestion: Suggestion | None
    callsite: Callsite | None
    observed_total_ms: float = 0.0
    fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "occurrences": self.occurrences,
            "observed_total_ms": round(self.observed_total_ms, 3),
            "fingerprint": self.fingerprint,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "routes": self.routes,
            "sample_trace_id": self.sample_trace_id,
            "sample_span_ids": self.sample_span_ids,
            "suggestion": self.suggestion.to_dict() if self.suggestion else None,
            "callsite": self.callsite.to_dict() if self.callsite else None,
        }


#: A period for :meth:`ExtendedStore.compare_periods`: ``("release", name)`` or
#: ``("range", start, end)`` (epoch seconds, end exclusive).
Period = tuple[str, str] | tuple[str, float, float]


def parse_period(text: str | None) -> Period:
    """``release:<name>`` or ``range:<start>-<end>`` (epoch seconds) → a :data:`Period`."""
    raw = (text or "").strip()
    kind, sep, rest = raw.partition(":")
    if sep and kind == "release" and rest.strip():
        return ("release", rest.strip())
    if sep and kind == "range":
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*", rest)
        if m and float(m.group(1)) < float(m.group(2)):
            return ("range", float(m.group(1)), float(m.group(2)))
    raise FilterError(
        f"Invalid period {text!r}; use release:<name> or range:<start>-<end> (epoch seconds)."
    )


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


@dataclass
class SqlStats:
    """One SQL fingerprint aggregated over a window (``GET api/sql``, the index advisor).

    ``fingerprint`` is the 16-hex :func:`redline_core.sql.fingerprint_hash`; ``sql`` is the
    normalised statement (literals replaced by ``?``). ``count`` counts executions, ``traces``
    the requests that ran it. Spans folded away by the span cap are not included.
    """

    fingerprint: str
    sql: str
    count: int
    traces: int
    total_ms: float
    max_ms: float
    routes: builtins.list[str]
    sample_trace_id: str
    vendor: str = "other"
    #: Mean rows per execution over the executions whose row count is known.
    mean_rows: float | None = None

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "sql": self.sql,
            "count": self.count,
            "traces": self.traces,
            "total_ms": round(self.total_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "mean_ms": round(self.mean_ms, 3),
            "mean_rows": _r(self.mean_rows, 2),
            "routes": self.routes,
            "sample_trace_id": self.sample_trace_id,
            "vendor": self.vendor,
        }


@dataclass
class CacheStats:
    """Cache usage per ``(backend, key_pattern)`` (``GET api/cache``).

    ``hit_rate`` is ``hits / (hits + misses)`` (``None`` when no read reported a hit or
    miss). ``unread_sets`` is the number of sets on a pattern that had no reads at all in the
    window. ``stampedes`` counts bursts: ≥ 3 different requests missing the same pattern
    within 1 s (request start times; each burst counted once).
    """

    backend: str
    key_pattern: str
    gets: int
    hits: int
    misses: int
    hit_rate: float | None
    sets: int
    deletes: int
    unread_sets: int
    stampedes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "key_pattern": self.key_pattern,
            "gets": self.gets,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": _r(self.hit_rate, 4),
            "sets": self.sets,
            "deletes": self.deletes,
            "unread_sets": self.unread_sets,
            "stampedes": self.stampedes,
        }


@dataclass
class ExternalStats:
    """Outbound HTTP per ``(method, url_pattern)`` (``GET api/external``).

    ``p95_ms`` comes from a histogram where each call is taken at its request's mean
    duration for that pattern (exact when a request calls the pattern once). ``errors`` are
    calls answered 5xx or 429 or without a response. ``duplicates`` counts requests that
    called the pattern ≥ 2 times, ``retries`` requests with ≥ 3 calls and ≥ 1 error.
    """

    method: str
    host: str
    url_pattern: str
    count: int
    total_ms: float
    p95_ms: float
    max_ms: float
    errors: int
    duplicates: int
    retries: int

    @property
    def error_rate(self) -> float:
        return self.errors / self.count if self.count else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "host": self.host,
            "url_pattern": self.url_pattern,
            "count": self.count,
            "total_ms": round(self.total_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "duplicates": self.duplicates,
            "retries": self.retries,
        }


@dataclass
class History:
    """``GET api/history``: ``series`` of ``{t, count, p50_ms, p95_ms, p99_ms, error_rate,
    avg_queries}`` (non-empty buckets only) and the releases first seen in the range."""

    series: builtins.list[dict[str, Any]]
    releases: builtins.list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"series": self.series, "releases": self.releases}


@dataclass
class ReleaseInfo:
    release: str
    first_seen: float
    last_seen: float
    requests: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "release": self.release,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "requests": self.requests,
        }


@dataclass
class PeriodStats:
    """One endpoint over one period of :meth:`ExtendedStore.compare_periods`."""

    count: int
    error_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    avg_ms: float
    avg_queries: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "error_rate": round(self.error_rate, 4),
            "p50_ms": round(self.p50_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "avg_ms": round(self.avg_ms, 3),
            "avg_queries": round(self.avg_queries, 2),
        }


@dataclass
class PeriodComparison:
    """``GET api/history/compare``. ``a`` is the baseline, ``b`` the period under test."""

    a: dict[str, Any]
    b: dict[str, Any]
    endpoints: builtins.list[dict[str, Any]]
    issues: dict[str, builtins.list[dict[str, Any]]]

    def to_dict(self) -> dict[str, Any]:
        return {"a": self.a, "b": self.b, "endpoints": self.endpoints, "issues": self.issues}


@dataclass
class IssueIndexEntry:
    """An issue's long-lived history (survives trace pruning)."""

    id: str
    kind: str
    severity: str
    title: str
    route: str
    first_seen: float
    last_seen: float
    first_release: str | None
    last_release: str | None
    occurrences: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "route": self.route,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "first_release": self.first_release,
            "last_release": self.last_release,
            "occurrences": self.occurrences,
        }


@dataclass
class Bottlenecks:
    """``GET api/runs/{id}/bottlenecks``: what grew as the load stepped up.

    ``items``: ``{key, kind: "sql" | "route", label, callsite, per_step: [{step, mean_ms,
    p95_ms, count}], growth}`` sorted by ``growth`` (last step's mean / first step's mean).
    """

    steps: builtins.list[dict[str, Any]]
    items: builtins.list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"steps": self.steps, "items": self.items}


@runtime_checkable
class Store(Protocol):
    """Where traces live. ``save`` must never block the request path."""

    path: str

    def save(self, trace: Trace) -> None: ...
    def get(self, trace_id: str, *, include_profile: bool = True) -> Trace | None: ...
    def get_profile(self, trace_id: str) -> dict[str, Any] | None: ...
    def has_profile(self, trace_id: str) -> bool: ...
    def list(
        self, filters: Filters | None = None, limit: int = 50, cursor: str | None = None
    ) -> Page: ...
    def endpoints(self, window: float) -> builtins.list[EndpointStats]: ...
    def issues(self, window: float, kind: str | None = None) -> builtins.list[IssueGroup]: ...
    def delete(self, trace_id: str) -> int: ...
    def clear(self) -> int: ...
    def prune(self, max_age_s: float | None = None, max_rows: int | None = None) -> int: ...
    def count(self) -> int: ...
    def flush(self, timeout: float | None = None) -> bool: ...
    def close(self) -> None: ...


@runtime_checkable
class ExtendedStore(Store, Protocol):
    """The v0.2 analytics, history and load-run surface (``CONTRACT.md`` §11).

    Optional: a custom store implementing only :class:`Store` keeps working, and the API
    answers 501 ``not_supported`` for an endpoint whose method is missing or raises
    ``NotImplementedError``. ``window`` arguments are seconds back from now.
    """

    def search_sql(
        self, q: str | None, window: float, limit: int = 100
    ) -> builtins.list[SqlStats]: ...
    def sql_workload(self, window: float) -> builtins.list[SqlStats]: ...
    def cache_stats(self, window: float) -> builtins.list[CacheStats]: ...
    def external_stats(self, window: float) -> builtins.list[ExternalStats]: ...
    def history(
        self,
        route: str | None,
        method: str | None,
        start: float,
        end: float,
        bucket: float = 3_600.0,
    ) -> History: ...
    def releases(self) -> builtins.list[ReleaseInfo]: ...
    def compare_periods(self, a: Period, b: Period) -> PeriodComparison: ...
    def issue_index(self, window: float | None = None) -> builtins.list[IssueIndexEntry]: ...
    def record_lifecycle(self, metrics, *, release: str | None = None) -> None: ...
    def lifecycle(self, window: float, limit: int = 200) -> builtins.list[dict[str, Any]]: ...
    def save_run(self, run: Run) -> None: ...
    def save_run_step(self, run_id: str, step: RunStep) -> None: ...
    def finish_run(
        self, run_id: str, status: str, summary: dict[str, Any] | None = None
    ) -> None: ...
    def get_run(self, run_id: str) -> Run | None: ...
    def list_runs(self, limit: int = 50) -> builtins.list[Run]: ...
    def delete_run(self, run_id: str) -> int: ...
    def run_bottlenecks(self, run_id: str) -> Bottlenecks | None: ...
    def traces_since(
        self,
        started_at: float,
        trace_id: str = "",
        limit: int = 100,
        after_seq: int | None = None,
    ) -> builtins.list[dict[str, Any]]: ...
