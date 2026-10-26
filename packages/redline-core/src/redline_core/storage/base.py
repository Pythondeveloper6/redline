"""The :class:`Store` protocol and the value types it returns."""

from __future__ import annotations

import base64
import binascii
import builtins
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..model import Callsite, Suggestion, Trace

__all__ = [
    "WINDOWS",
    "EndpointStats",
    "FilterError",
    "Filters",
    "IssueGroup",
    "Page",
    "Store",
    "decode_cursor",
    "encode_cursor",
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

        f = cls(q=text("q"), route=text("route"))
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "occurrences": self.occurrences,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "routes": self.routes,
            "sample_trace_id": self.sample_trace_id,
            "sample_span_ids": self.sample_span_ids,
            "suggestion": self.suggestion.to_dict() if self.suggestion else None,
            "callsite": self.callsite.to_dict() if self.callsite else None,
        }


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
