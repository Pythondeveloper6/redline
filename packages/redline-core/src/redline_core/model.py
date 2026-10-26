"""The recorded data model: :class:`Trace` → :class:`Span` tree, plus :class:`Issue`.

One HTTP request becomes one :class:`Trace`. Everything that happened while serving it
(SQL queries, cache calls, outbound HTTP, template renders, user ``span()`` blocks) is a
flat list of :class:`Span` objects whose ``parent_id`` links rebuild the tree::

    Trace  GET /orders/42/  200  83.1 ms
    ├─ Span view      shop.views.order_detail
    │  ├─ Span sql    SELECT … FROM shop_order WHERE id = ?
    │  └─ Span sql    SELECT … FROM shop_orderitem WHERE order_id = ?   (×24 → N+1)
    └─ Span template  shop/order_detail.html

Every class round-trips through JSON with ``to_dict()`` / ``from_dict()``; the dict shape
is the wire format in ``CONTRACT.md`` §1–2 and is what the UI reads. Times are floats:
``start_ms`` is milliseconds since the trace started, ``duration_ms`` is milliseconds and
``started_at`` is a Unix timestamp in seconds.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, cast

__all__ = [
    "FRAMEWORKS",
    "ISSUE_KINDS",
    "SEVERITIES",
    "SPAN_KINDS",
    "Callsite",
    "Frame",
    "Framework",
    "Issue",
    "IssueKind",
    "Severity",
    "Span",
    "SpanKind",
    "Suggestion",
    "Trace",
    "new_id",
]

SpanKind = Literal["sql", "cache", "http_out", "template", "middleware", "view", "custom"]
IssueKind = Literal["n_plus_one", "duplicate_query", "slow_query", "too_many_queries"]
Severity = Literal["info", "warning", "critical"]
Framework = Literal["django", "fastapi", "other"]
Confidence = Literal["high", "medium", "low"]

SPAN_KINDS: tuple[str, ...] = (
    "sql",
    "cache",
    "http_out",
    "template",
    "middleware",
    "view",
    "custom",
)
ISSUE_KINDS: tuple[str, ...] = ("n_plus_one", "duplicate_query", "slow_query", "too_many_queries")
SEVERITIES: tuple[str, ...] = ("info", "warning", "critical")
FRAMEWORKS: tuple[str, ...] = ("django", "fastapi", "other")


def new_id() -> str:
    """A fresh 32-character hex id (``uuid4().hex``), used for traces and spans."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Frame:
    """One project stack frame."""

    file: str
    line: int
    function: str

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "function": self.function}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Frame:
        return cls(file=str(data["file"]), line=int(data["line"]), function=str(data["function"]))


@dataclass(frozen=True)
class Callsite:
    """The project frame that triggered a span, with up to 12 project frames of context.

    ``file`` is relative to the project root when possible; ``stack`` is innermost last
    and its last element is the callsite itself.
    """

    file: str
    line: int
    function: str
    code: str | None = None
    stack: tuple[Frame, ...] = ()

    @property
    def location(self) -> str:
        """``file:line``, the key detectors group by."""
        return f"{self.file}:{self.line}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "function": self.function,
            "code": self.code,
            "stack": [f.to_dict() for f in self.stack],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Callsite | None:
        if not data:
            return None
        return cls(
            file=str(data["file"]),
            line=int(data["line"]),
            function=str(data.get("function", "")),
            code=data.get("code"),
            stack=tuple(Frame.from_dict(f) for f in data.get("stack") or ()),
        )


@dataclass
class Span:
    """One timed operation inside a trace.

    ``data`` is kind-specific (see ``CONTRACT.md`` §1); for ``sql`` spans it carries
    ``sql``, ``params``, ``fingerprint``, ``db_alias``, ``vendor``, ``rows``, ``many`` and
    ``explainable``.
    """

    id: str
    parent_id: str | None
    kind: SpanKind
    name: str
    start_ms: float
    duration_ms: float
    callsite: Callsite | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "start_ms": self.start_ms,
            "duration_ms": self.duration_ms,
            "callsite": self.callsite.to_dict() if self.callsite else None,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Span:
        return cls(
            id=str(data["id"]),
            parent_id=data.get("parent_id"),
            kind=cast(SpanKind, data["kind"]),
            name=str(data.get("name", "")),
            start_ms=float(data.get("start_ms", 0.0)),
            duration_ms=float(data.get("duration_ms", 0.0)),
            callsite=Callsite.from_dict(data.get("callsite")),
            data=dict(data.get("data") or {}),
        )


@dataclass
class Suggestion:
    """A proposed fix for an :class:`Issue`, produced by a ``FixSuggester``."""

    summary: str
    code: str | None = None
    diff: str | None = None
    confidence: Confidence = "medium"
    docs_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "code": self.code,
            "diff": self.diff,
            "confidence": self.confidence,
            "docs_url": self.docs_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Suggestion | None:
        if not data:
            return None
        return cls(
            summary=str(data["summary"]),
            code=data.get("code"),
            diff=data.get("diff"),
            confidence=cast(Confidence, data.get("confidence", "medium")),
            docs_url=data.get("docs_url"),
        )


@dataclass
class Issue:
    """A problem a detector found in one trace.

    ``id`` is stable for the same problem across requests (see
    :func:`redline_core.detect.issue_id`), which is how the Issues page deduplicates.
    """

    id: str
    kind: IssueKind
    severity: Severity
    title: str
    detail: str
    span_ids: list[str] = field(default_factory=list)
    fingerprint: str | None = None
    callsite: Callsite | None = None
    count: int = 0
    total_ms: float = 0.0
    suggestion: Suggestion | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "span_ids": list(self.span_ids),
            "fingerprint": self.fingerprint,
            "callsite": self.callsite.to_dict() if self.callsite else None,
            "count": self.count,
            "total_ms": self.total_ms,
            "suggestion": self.suggestion.to_dict() if self.suggestion else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Issue:
        return cls(
            id=str(data["id"]),
            kind=cast(IssueKind, data["kind"]),
            severity=cast(Severity, data["severity"]),
            title=str(data.get("title", "")),
            detail=str(data.get("detail", "")),
            span_ids=[str(s) for s in data.get("span_ids") or ()],
            fingerprint=data.get("fingerprint"),
            callsite=Callsite.from_dict(data.get("callsite")),
            count=int(data.get("count", 0)),
            total_ms=float(data.get("total_ms", 0.0)),
            suggestion=Suggestion.from_dict(data.get("suggestion")),
        )


def _empty_request() -> dict[str, Any]:
    return {"headers": {}, "query": {}, "body": None, "body_truncated": False, "client": None}


def _empty_response() -> dict[str, Any]:
    return {"headers": {}, "body": None, "body_truncated": False, "size": None}


@dataclass
class Trace:
    """One recorded request."""

    id: str
    framework: Framework
    method: str
    path: str
    route: str | None = None
    view: str | None = None
    status: int = 0
    started_at: float = 0.0
    duration_ms: float = 0.0
    query_count: int = 0
    query_ms: float = 0.0
    request: dict[str, Any] = field(default_factory=_empty_request)
    response: dict[str, Any] = field(default_factory=_empty_response)
    spans: list[Span] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    profile: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    # derived ---------------------------------------------------------------
    @property
    def has_profile(self) -> bool:
        return self.profile is not None

    @property
    def issue_kinds(self) -> list[str]:
        """Distinct issue kinds, in first-seen order."""
        return list(dict.fromkeys(i.kind for i in self.issues))

    def span(self, span_id: str) -> Span | None:
        """The span with ``span_id``, or ``None``."""
        for s in self.spans:
            if s.id == span_id:
                return s
        return None

    def sql_spans(self) -> list[Span]:
        return [s for s in self.spans if s.kind == "sql"]

    # serialisation ---------------------------------------------------------
    def to_dict(self, *, include_profile: bool = True) -> dict[str, Any]:
        """The full wire representation.

        With ``include_profile=False`` the (potentially large) speedscope document is
        omitted and replaced by ``has_profile`` – the shape of ``GET api/traces/{id}``.
        """
        data: dict[str, Any] = {
            "id": self.id,
            "framework": self.framework,
            "method": self.method,
            "path": self.path,
            "route": self.route,
            "view": self.view,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "query_count": self.query_count,
            "query_ms": self.query_ms,
            "request": self.request,
            "response": self.response,
            "spans": [s.to_dict() for s in self.spans],
            "issues": [i.to_dict() for i in self.issues],
            "tags": dict(self.tags),
            "error": self.error,
        }
        if include_profile:
            data["profile"] = self.profile
        else:
            data["has_profile"] = self.has_profile
        return data

    def summary(self) -> dict[str, Any]:
        """The ``TraceSummary`` shape used by the request list."""
        return {
            "id": self.id,
            "method": self.method,
            "path": self.path,
            "route": self.route,
            "view": self.view,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "query_count": self.query_count,
            "query_ms": self.query_ms,
            "issue_count": len(self.issues),
            "issue_kinds": self.issue_kinds,
            "has_profile": self.has_profile,
            "tags": dict(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Trace:
        request = _empty_request()
        request.update(data.get("request") or {})
        response = _empty_response()
        response.update(data.get("response") or {})
        return cls(
            id=str(data["id"]),
            framework=cast(Framework, data.get("framework", "other")),
            method=str(data.get("method", "")),
            path=str(data.get("path", "")),
            route=data.get("route"),
            view=data.get("view"),
            status=int(data.get("status", 0)),
            started_at=float(data.get("started_at", 0.0)),
            duration_ms=float(data.get("duration_ms", 0.0)),
            query_count=int(data.get("query_count", 0)),
            query_ms=float(data.get("query_ms", 0.0)),
            request=request,
            response=response,
            spans=[Span.from_dict(s) for s in data.get("spans") or ()],
            issues=[Issue.from_dict(i) for i in data.get("issues") or ()],
            tags={str(k): str(v) for k, v in (data.get("tags") or {}).items()},
            profile=data.get("profile"),
            error=data.get("error"),
        )
