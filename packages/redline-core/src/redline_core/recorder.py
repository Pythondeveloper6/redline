"""The recorder: the current trace and span, held in :mod:`contextvars`.

Two context variables carry all state – the :class:`TraceBuilder` of the request being
recorded, and the id of the innermost open :func:`span`. Because they are context
variables, recording is correct wherever Python propagates context:

* every ``asyncio`` task copies the context of the code that created it, so concurrent
  requests on one event loop never see each other's trace, and tasks spawned *inside* a
  request record into that request;
* ``asgiref.sync_to_async``, ``loop.run_in_executor`` via ``contextvars.copy_context()``,
  and Starlette's threadpool all copy context into the worker thread;
* a plain ``threading.Thread`` does **not** copy context, so work done there is not
  attributed to any request (by design: it isn't part of the request).

When no trace is active every hook is one ``ContextVar.get()`` and a ``None`` check.

Adapters drive it like this::

    builder = recorder.start_trace(framework="django", method="GET", path="/orders/42/",
                                   headers=request.headers, query=request.GET)
    try:
        with span("shop.views.order_detail", kind="view"):
            response = view(request)
    finally:
        trace = recorder.finish_trace(builder, status=response.status_code, ...)

and their database hooks call :func:`record_query` (or :func:`record_span`). The finished
trace is redacted, run through the detectors, sampled and handed to
``Recorder.store.save()``, which only enqueues – disk I/O happens on a background thread.
"""

from __future__ import annotations

import functools
import inspect
import json
import linecache
import logging
import re
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any, TypeVar, overload

from . import sql as sqlmod
from .callsite import CallsiteCapturer
from .conf import Settings, resolve_release
from .detect import Detector, FixSuggester
from .diagnostics import capture_failure as _capture_failure
from .model import Callsite, Frame, Framework, LogRecord, Span, SpanKind, Trace, new_id
from .redact import MASK, Redactor
from .sampling import Sampler

if TYPE_CHECKING:
    from .storage import Store

__all__ = [
    "OverflowGroup",
    "Recorder",
    "SpanContext",
    "TraceBuilder",
    "configure",
    "current",
    "finish_trace",
    "get_recorder",
    "record_query",
    "record_span",
    "span",
    "start_trace",
]

logger = logging.getLogger("redline")


F = TypeVar("F", bound=Callable[..., Any])

_trace: ContextVar[TraceBuilder | None] = ContextVar("redline_trace", default=None)
_parent: ContextVar[str | None] = ContextVar("redline_span", default=None)

_perf = time.perf_counter
_MAX_MANY_ROWS = 10  # executemany: keep the first N parameter rows
#: Span kinds never dropped by ``max_spans_per_trace`` (a handful per request, and the
#: waterfall's skeleton).
_ALWAYS_KEPT = frozenset(("middleware", "view"))
#: Distinct (fingerprint, callsite) groups tracked for spans beyond the cap.
_MAX_OVERFLOW_GROUPS = 1_000
_MAX_ERROR_FRAMES = 20  # project frames kept in ``Trace.error["frames"]``
_MAX_LOCALS = 30  # locals kept per frame
_MAX_REPR = 300  # chars kept per local
_MAX_LOG_CHARS = 2_000  # chars kept per captured log message


class OverflowGroup:
    """Counters for the SQL spans of one ``(fingerprint, callsite)`` dropped by the cap.

    The detectors add these to the stored spans, so an N+1 loop that runs past
    ``max_spans_per_trace`` is still reported with its full count and time.
    """

    __slots__ = ("count", "explainable", "max_ms", "sample", "slow_count", "slow_ms", "total_ms")

    def __init__(self, sample: Span) -> None:
        self.sample = sample  # the first dropped span, for callsite and suggesters
        self.explainable = bool(sample.data.get("read_query", sample.data.get("explainable")))
        self.count = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.slow_count = 0
        self.slow_ms = 0.0

    def add(self, duration_ms: float, slow_ms: float) -> None:
        self.count += 1
        self.total_ms += duration_ms
        if duration_ms > self.max_ms:
            self.max_ms = duration_ms
        if duration_ms >= slow_ms:
            self.slow_count += 1
            self.slow_ms += duration_ms


def current() -> TraceBuilder | None:
    """The trace being recorded in this context, or ``None``."""
    builder = _trace.get()
    return None if builder is None or builder.finished else builder


class TraceBuilder:
    """A trace under construction. Created by :meth:`Recorder.start_trace`."""

    __slots__ = (
        "_lock",
        "_parent_token",
        "_token",
        "active",
        "capture_error_details",
        "capture_errors",
        "capture_limited",
        "client",
        "connection_errors",
        "dropped_spans",
        "finished",
        "framework",
        "id",
        "job_outcomes",
        "logs",
        "logs_dropped",
        "max_spans",
        "method",
        "nested_error",
        "on_finish",
        "orm_instances",
        "overflow",
        "path",
        "path_params",
        "profiler",
        "query_count",
        "query_ms",
        "recorder",
        "req_body",
        "req_headers",
        "req_query",
        "retained_bytes",
        "rolled",
        "route",
        "spans",
        "sql_errors",
        "started_at",
        "t0",
        "tags",
        "view",
    )

    def __init__(
        self,
        recorder: Recorder,
        *,
        framework: Framework,
        method: str,
        path: str,
        route: str | None,
        view: str | None,
        headers: Any,
        query: Any,
        body: Any,
        client: str | None,
        tags: Mapping[str, str] | None,
        rolled: bool,
        active: bool,
    ) -> None:
        self.recorder = recorder
        self.id = new_id()
        self.framework = framework
        self.method = method.upper()
        self.path = path
        #: The route's parameters (``{"token": "…"}``), used to mask secret path segments.
        self.path_params: Mapping[str, Any] | None = None
        self.route = route
        self.view = view
        self.nested_error = None
        self.sql_errors = 0
        self.connection_errors = 0
        self.req_headers = headers
        self.req_query = query
        self.req_body = body
        self.client = client
        self.capture_errors = 0
        # Per-recording safety override. This may suppress configured detail capture,
        # but deliberately cannot opt a recording into capture when settings disable it.
        self.capture_error_details: bool | None = None
        self.capture_limited = False
        self.retained_bytes = 0
        self.job_outcomes = {}
        self.tags: dict[str, str] = {}
        if type(tags) is dict:
            from itertools import islice

            for key, value in islice(tags.items(), 100):
                self.tag(key, value)
            self.capture_limited |= len(tags) > 100
        elif tags is not None:
            self.capture_limited = True
        self.rolled = rolled
        self.active = active
        self.finished = False
        self.spans: list[Span] = []
        self.orm_instances = 0
        self.query_count = 0
        self.query_ms = 0.0
        self.max_spans = recorder.settings.max_spans_per_trace or 0
        self.dropped_spans = 0
        self.logs: list[LogRecord] = []
        self.logs_dropped = 0
        self.overflow: dict[tuple[str, str | None], OverflowGroup] = {}
        self.started_at = time.time()
        self.t0 = _perf()
        self.profiler: Any = None
        #: Callbacks run at the start of :meth:`Recorder.finish_trace` (e.g. the event-loop
        #: monitor flushing a stall that is still in progress); ``None`` when there are none.
        self.on_finish: list[Callable[[TraceBuilder], None]] | None = None
        self._lock = threading.Lock()
        self._token: Token[TraceBuilder | None] | None = None
        self._parent_token: Token[str | None] | None = None

    def __repr__(self) -> str:
        return f"<TraceBuilder {self.method} {self.path} spans={len(self.spans)}>"

    # mutation from adapters / user code -------------------------------------
    def set_route(
        self,
        route: str | None,
        view: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        """Record the resolved route template, view and path parameters (known only after
        routing). Parameters with secret-looking names are masked in the stored path."""
        if route is not None:
            self.route = route
        if view is not None:
            self.view = view
        if params is not None:
            self.path_params = params

    def tag(self, key: str, value: Any) -> None:
        from .capture_limits import bounded_value

        if type(key) is not str or len(key) > 128:
            self.capture_limited = True
            return
        if key not in self.tags and len(self.tags) >= 100:
            self.capture_limited = True
            return
        safe, cut = bounded_value(
            value,
            max_bytes=self.recorder.settings.max_tag_bytes,
            max_string_bytes=self.recorder.settings.max_tag_bytes,
            max_items=20,
        )
        text = (
            safe
            if type(safe) is str
            else json.dumps(safe, ensure_ascii=True, separators=(",", ":"))
        )
        self.tags[key] = text
        self.capture_limited |= cut

    def elapsed_ms(self) -> float:
        return (_perf() - self.t0) * 1000.0

    def server_timing(self) -> str:
        """The ``Server-Timing`` header value for the request so far (``CONTRACT.md`` §14).

        ``total;dur=…, db;dur=…;desc="12 queries", cache;dur=…, http;dur=…, tpl;dur=…,
        redline;desc="<trace id>"`` with durations in ms (one decimal); groups with no
        spans are left out. Empty for an inert builder; never raises.
        """
        if not self.active:
            return ""
        try:
            with self._lock:
                spans = list(self.spans)
                queries = self.query_count
                query_ms = self.query_ms
            sums = {"cache": 0.0, "http_out": 0.0, "template": 0.0}
            seen: set[str] = set()
            for s in spans:
                if s.kind in sums:
                    sums[s.kind] += s.duration_ms
                    seen.add(s.kind)
            parts = [f"total;dur={self.elapsed_ms():.1f}"]
            if queries:
                noun = "query" if queries == 1 else "queries"
                parts.append(f'db;dur={query_ms:.1f};desc="{queries} {noun}"')
            for kind, label in (("cache", "cache"), ("http_out", "http"), ("template", "tpl")):
                if kind in seen:
                    parts.append(f"{label};dur={sums[kind]:.1f}")
            parts.append(f'redline;desc="{self.id}"')
            return ", ".join(parts)
        except Exception:  # pragma: no cover - a header must never break a response
            return ""

    @property
    def full(self) -> bool:
        """Whether the ``max_spans_per_trace`` cap has been reached."""
        return (
            bool(self.max_spans) and len(self.spans) >= self.max_spans
        ) or self.retained_bytes >= self.recorder.settings.max_trace_bytes

    def add(self, span: Span) -> None:
        """Append a finished span (thread-safe).

        Beyond ``max_spans_per_trace`` the span is not stored – it is counted, and SQL
        spans are folded into per-(fingerprint, callsite) :class:`OverflowGroup`
        counters so query totals and N+1 detection stay exact.
        """
        estimate = (
            len(json.dumps(span.data, ensure_ascii=True, separators=(",", ":")).encode())
            + len(span.name.encode())
            + 2048
        )
        with self._lock:
            within_bytes = self.retained_bytes + estimate <= self.recorder.settings.max_trace_bytes
            if span.kind == "sql":
                self.query_count += 1
                self.query_ms += span.duration_ms
            if within_bytes and (
                not self.max_spans or len(self.spans) < self.max_spans or span.kind in _ALWAYS_KEPT
            ):
                self.spans.append(span)
                self.retained_bytes += estimate
                return
            self.capture_limited |= not within_bytes
            self.dropped_spans += 1
            if span.kind != "sql":
                return
            loc = span.callsite.location if span.callsite else None
            key = (str(span.data.get("fingerprint", "")), loc)
            group = self.overflow.get(key)
            if group is None:
                if len(self.overflow) >= _MAX_OVERFLOW_GROUPS or not within_bytes:
                    return
                group = self.overflow[key] = OverflowGroup(span)
                self.retained_bytes += estimate
            group.add(span.duration_ms, self.recorder.settings.slow_query_ms)


class Recorder:
    """Ties settings, sampling, redaction, detection and storage together."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: Store | None = None,
        suggesters: Sequence[FixSuggester] = (),
        *,
        capturer: CallsiteCapturer | None = None,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.store = store
        self.dropped_capture = 0
        self.sampler = Sampler(self.settings, rng) if rng else Sampler(self.settings)
        self.redactor = Redactor(self.settings)
        self.detector = Detector(self.settings, suggesters)
        self.capturer = capturer or CallsiteCapturer(self.settings.project_root)
        #: ``Settings.release`` or the automatic value, resolved once.
        self.release = resolve_release(self.settings)
        self._log_level = getattr(logging, self.settings.log_level, logging.INFO)
        self._capture_logs = self.settings.capture_logs and self.settings.feature("logs")
        if self.settings.enabled and self._capture_logs:
            _install_log_capture()
        #: The trace most recently finished by :meth:`recording` (handy in tests).
        self.last_trace: Trace | None = None
        if self.settings.enabled and self.settings.instrument_engines == "all":
            from .contrib.sqlalchemy import instrument_all_engines

            instrument_all_engines()

    # lifecycle -------------------------------------------------------------
    def should_record(self, path: str) -> bool:
        """``False`` when Redline is disabled or ``path`` is in ``ignore_paths``."""
        return not self.sampler.ignored(path)

    def start_trace(
        self,
        *,
        method: str,
        path: str,
        framework: Framework = "other",
        route: str | None = None,
        view: str | None = None,
        headers: Any = None,
        query: Any = None,
        body: bytes | str | None = None,
        client: str | None = None,
        tags: Mapping[str, str] | None = None,
        profile_requested: bool = False,
    ) -> TraceBuilder:
        """Begin recording a request in the current context.

        ``headers`` may be a mapping or ASGI-style ``[(b"name", b"value")]``; ``query`` a
        mapping or raw query string; ``body`` the raw request body. All of it is redacted
        in :meth:`finish_trace`, never stored raw. For an ignored path the returned
        builder is inert (nothing is recorded or stored).
        """
        active = self.should_record(path)
        builder = TraceBuilder(
            self,
            framework=framework,
            method=method,
            path=path,
            route=route,
            view=view,
            headers=headers,
            query=query,
            body=body if self.settings.capture_request_body else None,
            client=client,
            tags=tags,
            rolled=self.sampler.roll() if active else False,
            active=active,
        )
        if not active:
            return builder
        _apply_run_header(builder, headers)
        from .privacy import correlate

        correlation = Trace(builder.id, framework, method, path)
        correlate(correlation, headers, None, self.settings)
        builder.tags.update(correlation.tags)
        builder._token = _trace.set(builder)
        builder._parent_token = _parent.set(None)
        if self.sampler.wants_profile(profile_requested):
            try:
                from .profiler import RequestProfiler

                prof = RequestProfiler()
                prof.start()
                builder.profiler = prof
            except Exception as exc:  # missing extra, or another profiler running
                builder.tags["profile_error"] = type(exc).__name__
        return builder

    def finish_trace(
        self,
        builder: TraceBuilder,
        *,
        status: int,
        headers: Any = None,
        body: bytes | str | None = None,
        size: int | None = None,
        route: str | None = None,
        view: str | None = None,
        error: BaseException | Mapping[str, Any] | None = None,
        path_params: Mapping[str, Any] | None = None,
        request_object: Any = None,
    ) -> Trace:
        """Finish observation without changing the application's result or exception."""
        try:
            return self._finish_trace(
                builder,
                status=status,
                headers=headers,
                body=body,
                size=size,
                route=route,
                view=view,
                error=error,
                path_params=path_params,
                request_object=request_object,
            )
        except Exception:
            _capture_failure("Redline failed to finish observation")
            builder.finished = True
            _reset(builder)
            if builder.profiler is not None:
                try:
                    builder.profiler.stop("[capture-unavailable]")
                except Exception:
                    pass
            from .trace_safety import enforce_trace_limits, minimal_trace

            seed = Trace(builder.id, builder.framework, builder.method, "")
            seed.status = status if type(status) is int else 0
            seed.started_at = builder.started_at
            seed.query_count = builder.query_count
            seed.query_ms = builder.query_ms
            seed.tags = {
                key: str(getattr(builder, key))
                for key in ("sql_errors", "connection_errors")
                if getattr(builder, key)
            }
            trace = minimal_trace(seed, "capture_failure")
            self.dropped_capture += 1
            if builder.active:
                from .testing import active_capture

                capture = active_capture()
                if capture is not None:
                    capture[1].append(trace)
                safe = enforce_trace_limits(trace, self.settings)
                if safe is not None and self.store is not None:
                    try:
                        self.store.save(safe)
                    except Exception:
                        _capture_failure("Redline failed to enqueue failure metadata")
            return trace

    def _finish_trace(
        self,
        builder: TraceBuilder,
        *,
        status: int,
        headers: Any = None,
        body: bytes | str | None = None,
        size: int | None = None,
        route: str | None = None,
        view: str | None = None,
        error: BaseException | Mapping[str, Any] | None = None,
        path_params: Mapping[str, Any] | None = None,
        request_object: Any = None,
    ) -> Trace:
        """Stop recording and return the finished trace (stored if sampling keeps it).

        Response ``headers``/``body`` are redacted like the request's. ``error`` is the
        unhandled exception, if any. ``path_params`` are the route's parameters: values of
        secret-looking ones are masked in the stored path (:meth:`Redactor.path`).
        """
        if request_object is not None:
            from .privacy import resolve_user

            resolve_user(builder, request_object)
        if builder.on_finish:
            for callback in builder.on_finish:
                try:
                    callback(builder)
                except Exception:  # a hook must never break the response
                    _capture_failure("Redline finish hook failed")
            builder.on_finish = None
        error = error or builder.nested_error
        if builder.tags.get("nested.framework"):
            route, view = builder.route, builder.view
        end = _perf()
        builder.finished = True
        _reset(builder)
        builder.set_route(route, view, path_params)
        try:
            builder.path = self.redactor.path(
                builder.path, builder.path_params, resolved=builder.route is not None
            )
        except Exception:  # pragma: no cover - defensive
            # Returning application paths after a privacy failure is unsafe.
            raise
        profile = None
        if builder.profiler is not None:
            title = f"{builder.method} {builder.route or builder.path}"
            if builder.view:
                title += f" \u2014 {builder.view}"
            try:
                profile = builder.profiler.stop(title)
            except Exception as exc:  # pragma: no cover - defensive
                builder.tags["profile_error"] = type(exc).__name__
        duration = (end - builder.t0) * 1000.0
        trace = Trace(
            id=builder.id,
            framework=builder.framework,
            method=builder.method,
            path=builder.path,
            route=builder.route,
            view=builder.view,
            status=int(status),
            started_at=builder.started_at,
            duration_ms=round(duration, 3),
            tags=dict(builder.tags),
            profile=profile,
            error=_error_dict(
                error,
                self.redactor,
                capture_details=self.settings.capture_error_details
                and builder.capture_error_details is not False,
            ),
            release=self.release,
        )
        for key in ("sql_errors", "connection_errors"):
            count = getattr(builder, key, 0)
            if count:
                trace.tags[key] = str(count)
            else:
                trace.tags.pop(key, None)
        error_details_enabled = (
            self.settings.capture_error_details and builder.capture_error_details is not False
        )
        if (
            isinstance(error, BaseException)
            and trace.error
            and builder.capture_error_details is not False
        ):
            tb = error.__traceback__
            while tb is not None:
                file = self.capturer.classify(tb.tb_frame.f_code.co_filename)
                if file is not None:
                    trace.error["origin"] = {
                        "file": file,
                        "line": tb.tb_lineno,
                        "function": tb.tb_frame.f_code.co_name,
                    }
                tb = tb.tb_next
        if not builder.active:
            return trace
        with builder._lock:
            trace.logs = list(builder.logs)
            trace.logs_dropped = builder.logs_dropped
            spans = sorted(builder.spans, key=lambda s: s.start_ms)
            if builder.orm_instances:
                trace.tags["orm_instances"] = str(builder.orm_instances)
            trace.query_count = builder.query_count
            trace.query_ms = round(builder.query_ms, 3)
            overflow = list(builder.overflow.values())
            if builder.capture_limited or builder.capture_errors:
                trace.truncated = {
                    "capture_limits": builder.capture_limited,
                    "capture_errors": builder.capture_errors,
                    "incomplete_evidence": True,
                }
            if builder.dropped_spans:
                trace.truncated = {
                    **(trace.truncated or {}),
                    "dropped_spans": builder.dropped_spans,
                    "max_spans": builder.max_spans,
                }
        if trace.status >= 400 and any(
            s.kind == "transaction" and s.data.get("outcome") == "commit" for s in spans
        ):
            trace.tags["commit_observed_before_failure"] = "true"
        trace.spans = spans
        from .privacy import correlate

        correlate(trace, builder.req_headers, headers, self.settings)
        reason = self.sampler.keep(
            rolled=builder.rolled,
            status=trace.status,
            duration_ms=duration,
            error=trace.error is not None
            or bool(trace.tags.get("sql_errors"))
            or bool(trace.tags.get("connection_errors"))
            or any(s.data.get("error") for s in spans if s.kind == "sql"),
            # an explicitly profiled request is kept even when the profiler caught no
            # sample (a very fast request), so the keep rule does not depend on timing
            profiled=builder.profiler is not None,
            # a refactor-check call is kept provisionally only when the store validates
            # the tag (its writer drops it for an unknown or finished check run)
            check="check_run_id" in trace.tags
            and getattr(self.store, "validates_check_tags", False) is True,
        )
        from .testing import active_capture

        self._persist_lifecycle()
        capture = active_capture()
        if capture is not None:
            reason = reason or "test"
            if capture[0]:
                trace.tags["test_name"] = self.redactor.text(capture[0])
        if reason is None:
            return trace
        from .runtime_info import target_runtime

        trace.runtime = dict(target_runtime(trace.framework))
        trace.tags["sampled_by"] = reason
        if (
            isinstance(error, BaseException)
            and self.settings.capture_locals
            and error_details_enabled
            and self.settings.feature("locals")
            and trace.error
        ):
            try:
                trace.error["frames"] = self._error_frames(error)
            except Exception:  # never let analysis break the response
                _capture_failure("Redline failed to capture exception locals")
        from .trace_safety import enforce_trace_limits, minimal_trace

        try:
            trace.request = self._request_dict(builder)
            trace.response = self._response_dict(headers, body, size)
            from .privacy import sanitize_trace

            sanitize_trace(trace, self.settings)
        except Exception:
            _capture_failure("Redline privacy processing failed")
            trace = minimal_trace(trace, "privacy_failure")
        else:
            try:
                trace.issues = self.detector.detect(trace, overflow=overflow)
            except Exception:
                _capture_failure("Redline analysis failed")
                trace.issues = []
                trace.truncated = {
                    **(trace.truncated or {}),
                    "incomplete_evidence": True,
                    "analysis_failed": True,
                }
            try:
                from .privacy import scrub_issue_sources

                scrub_issue_sources(trace, self.settings)
            except Exception:
                _capture_failure("Redline issue privacy processing failed")
                trace = minimal_trace(trace, "privacy_failure")
        safe = enforce_trace_limits(trace, self.settings)
        if safe is None:
            safe = minimal_trace(trace, "capture_failure")
            self.dropped_capture += 1
            if capture is not None:
                capture[1].append(safe)
            return safe
        trace = safe
        if capture is not None:
            capture[1].append(trace)
        if self.store is not None:
            try:
                self.store.save(trace)
            except Exception:
                self.dropped_capture += 1
                _capture_failure("Redline failed to enqueue recording")
        return trace

    def _persist_lifecycle(self) -> None:
        """Flush bounded process gauges when the store supports persisted history."""
        if self.store is None:
            return
        record = getattr(self.store, "record_lifecycle", None)
        if not callable(record):
            return
        try:
            from .connection_health import snapshot

            metrics = snapshot()
            if metrics:
                record(metrics, release=self.settings.release)
        except Exception:
            _capture_failure("Redline failed to persist connection lifecycle metrics")

    @contextmanager
    def recording(self, **fields: Any) -> Iterator[TraceBuilder]:
        """``with recorder.recording(method="GET", path="/x") as b: ...`` – status 200, or
        500 when the block raises. The finished trace is ``recorder.last_trace``."""
        builder = self.start_trace(**fields)
        try:
            yield builder
        except BaseException as exc:
            self.last_trace = self.finish_trace(builder, status=500, error=exc)
            raise
        else:
            self.last_trace = self.finish_trace(builder, status=200)

    # exception locals ------------------------------------------------------
    def _error_frames(self, error: BaseException) -> list[dict[str, Any]]:
        """Project frames of ``error``'s traceback with their redacted locals."""
        frames: list[tuple[Any, int, str]] = []
        tb = error.__traceback__
        while tb is not None:
            display = self.capturer.classify(tb.tb_frame.f_code.co_filename)
            if display is not None:
                frames.append((tb.tb_frame, tb.tb_lineno, display))
            tb = tb.tb_next
        redactor = self.redactor
        out: list[dict[str, Any]] = []
        for frame, line, display in frames[-_MAX_ERROR_FRAMES:]:
            filename = frame.f_code.co_filename
            local_vars: dict[str, str] = {}
            for name, value in list(frame.f_locals.items()):
                if len(local_vars) >= _MAX_LOCALS:
                    break
                name = str(name)
                if name.startswith("__"):
                    continue
                if redactor.is_secret_key(name):
                    local_vars[name] = MASK
                    continue
                try:
                    text = _local_repr(redactor, value)
                except Exception:
                    text = "<unrepresentable>"
                local_vars[name] = text if len(text) <= _MAX_REPR else text[:_MAX_REPR] + "…"
            out.append(
                {
                    "file": display,
                    "line": line,
                    "function": frame.f_code.co_name,
                    "code": redactor.text(linecache.getline(filename, line).strip()) or None,
                    "locals": local_vars,
                }
            )
        return out

    # redaction helpers -----------------------------------------------------
    def _request_dict(self, b: TraceBuilder) -> dict[str, Any]:
        r = self.redactor
        headers = r.headers(b.req_headers)
        ctype = _header(headers, "content-type")
        text, truncated = r.body(b.req_body, ctype)
        return {
            "headers": headers,
            "query": r.query(b.req_query),
            "body": text,
            "body_truncated": truncated,
            "client": b.client,
        }

    def _response_dict(self, headers: Any, body: Any, size: int | None) -> dict[str, Any]:
        r = self.redactor
        hdrs = r.headers(headers)
        if size is None and body is not None:
            size = len(body.encode("utf-8") if isinstance(body, str) else body)
        text, truncated = (None, False)
        if self.settings.capture_response_body:
            # Response bodies are mostly HTML: the free-text scan would cost ~2 ms per 20 kB
            # page on the finish path, so only JSON/form/multipart keys are masked here.
            text, truncated = r.body(body, _header(hdrs, "content-type"), free_text=False)
        return {"headers": hdrs, "body": text, "body_truncated": truncated, "size": size}


# ── exception locals ─────────────────────────────────────────────────────────────────

_LOCAL_DEPTH = 4  # container nesting masked in a local's value
_LOCAL_ITEMS = 50  # items kept per container (the repr is cut at _MAX_REPR anyway)
_LOCAL_TEXT = 20_000  # chars of a str/bytes value or repr scanned for secrets


class _More:
    """Stands in for the items of a container beyond :data:`_LOCAL_ITEMS`."""

    def __repr__(self) -> str:
        return "…"


def _local_repr(redactor: Redactor, value: Any) -> str:
    """``repr(value)`` with secrets masked: keys matching ``redact_keys`` at any depth of
    dicts/lists/tuples, JSON and ``key=value`` secrets inside str/bytes, and
    finally the whole repr through :meth:`Redactor.text` (URLs, ``?token=…`` in reprs)."""
    text = repr(_mask_local(redactor, value, _LOCAL_DEPTH))
    return redactor.text(text[:_LOCAL_TEXT])


def _mask_local(redactor: Redactor, value: Any, depth: int) -> Any:
    if isinstance(value, str):
        return _mask_local_text(redactor, value)
    if isinstance(value, (bytes, bytearray)):
        try:
            decoded = bytes(value[:_LOCAL_TEXT]).decode("utf-8")
        except UnicodeDecodeError:
            return value
        masked = _mask_local_text(redactor, decoded)
        return value if masked == decoded else masked.encode("utf-8")
    if depth <= 0 or not isinstance(value, (Mapping, list, tuple)):
        return value
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _LOCAL_ITEMS:
                out["…"] = _More()
                break
            secret = isinstance(k, str) and redactor.is_secret_key(k)
            out[k] = MASK if secret else _mask_local(redactor, v, depth - 1)
        return out
    items = [_mask_local(redactor, v, depth - 1) for v in value[:_LOCAL_ITEMS]]
    if len(value) > _LOCAL_ITEMS:
        items.append(_More())
    return tuple(items) if isinstance(value, tuple) else items


def _mask_local_text(redactor: Redactor, text: str) -> str:
    """A str local with JSON secrets masked (like a body), then ``key=value`` pairs
    (which covers form-encoded text and query strings)."""
    text = text[:_LOCAL_TEXT]
    if text.lstrip()[:1] in ("{", "["):
        try:
            data = json.loads(text)
            masked = redactor.mapping(data)
            if masked != data:
                return json.dumps(masked, ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
    return redactor.text(text)


# ── log capture ──────────────────────────────────────────────────────────────────────


class _CaptureFactory:
    """``logging`` record factory that files records under the request being recorded.

    Installed with :func:`logging.setLogRecordFactory` around whatever factory was current,
    so Redline adds no handler and changes no logger level: the app's logging configuration
    (``basicConfig``, ``dictConfig``, ``force=True``) neither sees Redline nor can remove it.
    :mod:`logging` only creates records for enabled loggers, so capture follows the app's
    levels. With no active trace a record costs one ``ContextVar.get()``. Records from the
    ``redline`` loggers are ignored, so Redline's own logging can never recurse into it.
    """

    __slots__ = ("enabled", "previous")

    def __init__(self, previous: Callable[..., logging.LogRecord]) -> None:
        self.previous = previous
        #: ``False`` once uninstalled while another factory wraps this one: pass-through.
        self.enabled = True

    def __call__(self, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = self.previous(*args, **kwargs)
        if self.enabled:
            builder = _trace.get()
            if builder is not None:
                _capture_log(builder, record)
        return record


def _capture_log(builder: TraceBuilder, record: logging.LogRecord) -> None:
    if builder.finished or not builder.active:
        return
    try:
        _capture(builder, record)
    except Exception:  # logging must never break the app
        pass


def _capture(builder: TraceBuilder, record: logging.LogRecord) -> None:
    recorder = builder.recorder
    if not recorder._capture_logs or record.levelno < recorder._log_level:
        return
    name = record.name
    if not isinstance(name, str) or name == "redline" or name.startswith("redline."):
        return  # ``makeLogRecord`` builds a nameless record first; Redline's own logs
    if len(builder.logs) >= recorder.settings.max_logs_per_trace:
        with builder._lock:
            builder.logs_dropped += 1
        return
    try:
        message = record.getMessage()
    except Exception:
        message = str(record.msg)
    from fnmatch import fnmatchcase

    if recorder.settings.capture_loggers and not any(
        fnmatchcase(record.name, p) for p in recorder.settings.capture_loggers
    ):
        return
    if any(fnmatchcase(record.name, p) for p in recorder.settings.ignore_loggers):
        return
    message = recorder.redactor.text(message[:20_000])[:_MAX_LOG_CHARS]
    entry = LogRecord(
        t_ms=round(builder.elapsed_ms(), 3),
        level=record.levelname if record.levelname in _LEVELS else _level_name(record.levelno),
        logger=name,
        message=message,
        callsite=_log_callsite(recorder, record),
    )
    with builder._lock:
        if len(builder.logs) >= recorder.settings.max_logs_per_trace:
            builder.logs_dropped += 1
        else:
            builder.logs.append(entry)


_LEVELS = frozenset(("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))


def _level_name(levelno: int) -> str:
    for limit, name in ((50, "CRITICAL"), (40, "ERROR"), (30, "WARNING"), (20, "INFO")):
        if levelno >= limit:
            return name
    return "DEBUG"


def _log_callsite(recorder: Recorder, record: logging.LogRecord) -> Callsite | None:
    path, line = record.pathname, record.lineno
    if not path:
        return None
    display = recorder.capturer.classify(path)
    if display is None:  # library code: keep the path as logged
        return Callsite(file=path, line=line, function=record.funcName or "")
    function = record.funcName or ""
    return Callsite(
        file=display,
        line=line,
        function=function,
        code=linecache.getline(path, line).strip() or None,
        stack=(Frame(file=display, line=line, function=function),),
    )


_log_lock = threading.Lock()
_log_factory: _CaptureFactory | None = None


def _install_log_capture() -> None:
    """Wrap the current log record factory with the capture factory (idempotent)."""
    global _log_factory
    with _log_lock:
        if _log_factory is not None:
            return
        _log_factory = _CaptureFactory(logging.getLogRecordFactory())
        logging.setLogRecordFactory(_log_factory)


def _uninstall_log_capture() -> None:
    """Undo :func:`_install_log_capture`.

    The previous factory is restored when the capture factory is still the current one;
    when the app has wrapped it since, it stays in that chain but only passes through.
    """
    global _log_factory
    with _log_lock:
        factory, _log_factory = _log_factory, None
        if factory is None:
            return
        factory.enabled = False
        if logging.getLogRecordFactory() is factory:
            logging.setLogRecordFactory(factory.previous)


# ── module-level API (the default recorder) ──────────────────────────────────────────

_default: Recorder | None = None
_default_lock = threading.Lock()


def configure(
    settings: Settings | None = None,
    store: Store | None = None,
    suggesters: Sequence[FixSuggester] = (),
) -> Recorder:
    """Install the process-wide default :class:`Recorder` and return it."""
    global _default
    with _default_lock:
        _default = Recorder(settings, store, suggesters)
    return _default


def get_recorder() -> Recorder:
    """The default recorder (a storage-less one is created if none was configured)."""
    active = _trace.get()
    if active is not None and active.active and not active.finished:
        return active.recorder
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = Recorder()
    return _default


def start_trace(recorder: Recorder | None = None, **fields: Any) -> TraceBuilder:
    """:meth:`Recorder.start_trace` on ``recorder`` or the default recorder."""
    return (recorder or get_recorder()).start_trace(**fields)


def finish_trace(builder: TraceBuilder, **fields: Any) -> Trace:
    """:meth:`Recorder.finish_trace` on the builder's own recorder."""
    return builder.recorder.finish_trace(builder, **fields)


def record_span(
    kind: SpanKind,
    name: str,
    start: float,
    duration: float,
    data: dict[str, Any] | None = None,
    capture_callsite: bool = True,
    *,
    callsite: Callsite | None = None,
) -> Span | None:
    """Record a finished span on the current trace; a no-op (``None``) without one.

    ``start`` is the ``time.perf_counter()`` reading when the operation began and
    ``duration`` its length in **seconds** (``time.perf_counter() - start``). For ``sql``
    spans missing ``fingerprint``/``explainable`` are filled in – prefer
    :func:`record_query`, which also redacts params.
    """
    builder = _trace.get()
    if builder is None or builder.finished:
        return None
    from .capture_limits import bounded_value

    data, cut = bounded_value(
        data if data is not None else {},
        max_bytes=builder.recorder.settings.max_span_data_bytes,
        max_items=200,
        max_string_bytes=builder.recorder.settings.max_sql_bytes,
    )
    if type(data) is not dict:
        data, cut = {}, True
    if type(name) is str and len(name) <= 120 and name.isascii():
        name_cut = False
    else:
        name, name_cut = bounded_value(name, max_bytes=2048, max_string_bytes=1024)
    if type(name) is not str:
        name, name_cut = "[unavailable]", True
    if cut or name_cut:
        builder.capture_limited = True
        data["capture_incomplete"] = True
        if kind == "sql":
            data.update(explainable=False, params_captured=False, params=None)
    if kind == "sql":
        raw = data.get("sql", "")
        data.setdefault("fingerprint", sqlmod.fingerprint(raw))
        data.setdefault("explainable", sqlmod.is_explainable(raw))
    if callsite is None and capture_callsite:
        callsite = builder.recorder.capturer.capture(skip=2)
    span = Span(
        id=new_id(),
        parent_id=_parent.get(),
        kind=kind,
        name=name,
        start_ms=round(max(0.0, (start - builder.t0) * 1000.0), 3),
        duration_ms=round(max(0.0, duration * 1000.0), 3),
        callsite=callsite,
        data=data,
    )
    builder.add(span)
    return span


def record_query(
    sql: str,
    params: Any = None,
    *,
    start: float,
    duration: float,
    db_alias: str = "default",
    vendor: str = "other",
    rows: int | None = None,
    many: bool = False,
    capture_callsite: bool = True,
    honor_ignore: bool = True,
) -> Span | None:
    """Record one SQL execution (``start``/``duration`` as in :func:`record_span`).

    ``params`` are redacted (values bound to secret-looking columns are masked) and made
    JSON-safe; for ``many=True`` pass the parameter rows, of which the first 10 are kept.
    Statements matching the ``ignore_sql`` setting (session housekeeping) are not recorded
    and give ``None``, unless ``honor_ignore`` is false (failed statements stay visible).
    """
    builder = _trace.get()
    if builder is None or builder.finished:
        return None
    redactor = builder.recorder.redactor
    settings = builder.recorder.settings
    if honor_ignore and settings.ignore_sql and type(sql) is str:
        if sqlmod.is_ignored(sql, settings.ignore_sql):
            return None
    truncated_sql = type(sql) is not str
    if type(sql) is not str:
        sql = "UNKNOWN"
    if len(sql) > settings.max_sql_bytes // 4:
        prefix = sql[: settings.max_sql_bytes].encode("utf-8", errors="replace")
        limited_sql = prefix[: settings.max_sql_bytes].decode("utf-8", errors="ignore")
        truncated_sql |= limited_sql != sql
        sql = limited_sql
    policy = settings.capture_sql_params
    masked = policy is False or (policy == "reads_only" and not sqlmod.is_explainable(sql))
    if masked:
        sql = sqlmod.fingerprint(sql)
        params = None
    incomplete_params = False
    if builder.full or masked:
        safe = None
    elif many and type(params) in (list, tuple):
        safe = []
        for row in params[:_MAX_MANY_ROWS]:
            shown, cut = redactor.bounded_sql_params(
                sql, row, max_bytes=settings.max_span_data_bytes // 10
            )
            safe.append(shown)
            incomplete_params |= cut
        incomplete_params |= len(params) > _MAX_MANY_ROWS
    else:
        safe, incomplete_params = redactor.bounded_sql_params(
            sql, params, max_bytes=settings.max_span_data_bytes
        )
    if truncated_sql or incomplete_params:
        builder.capture_limited = True
    from .vendors import get_vendor

    fp = sqlmod.fingerprint(sql)
    data = {
        "sql": sql,
        "params": safe,
        "fingerprint": fp,
        "db_alias": db_alias,
        "vendor": get_vendor(vendor).name,
        "rows": rows,
        "many": many,
        "explainable": (not many)
        and not masked
        and not truncated_sql
        and not incomplete_params
        and sqlmod.is_explainable(sql),
        "params_captured": not masked and not incomplete_params,
        "sql_truncated": truncated_sql,
        "capture_incomplete": truncated_sql or incomplete_params,
        "read_query": sqlmod.is_explainable(sql),
        "has_limit": sqlmod.has_limit(sql),
    }
    procedure = sqlmod.procedure_name(sql)
    if procedure is not None:
        data["query_kind"] = "procedure"
        data["procedure"] = procedure
    else:
        data["query_kind"] = sqlmod.statement_kind(sql).lower()
    callsite = builder.recorder.capturer.capture(skip=2) if capture_callsite else None
    if masked and callsite is not None:
        from dataclasses import replace

        callsite = replace(callsite, code=None)
    return record_span(
        "sql",
        sqlmod.truncate_name(fp),
        start,
        duration,
        data,
        capture_callsite=False,
        callsite=callsite,
    )


# ── span(): user-facing context manager / decorator ──────────────────────────────────


class SpanContext:
    """Returned by :func:`span`; a sync/async context manager and a decorator.

    One instance must not be entered concurrently with itself – the decorator form
    creates a fresh instance per call, so decorated functions are safe everywhere.
    """

    __slots__ = ("_builder", "_id", "_prev", "_start", "_token", "data", "kind", "name")

    def __init__(
        self, name: str | None, kind: SpanKind = "custom", data: dict[str, Any] | None = None
    ):
        self.name = name
        self.kind = kind
        self.data = data or {}
        self._builder: TraceBuilder | None = None

    # context manager -------------------------------------------------------
    def __enter__(self) -> SpanContext:
        builder = _trace.get()
        if builder is not None and builder.finished:
            builder = None
        self._builder = builder
        if builder is not None:
            self._id = new_id()
            self._prev = _parent.get()
            self._token = _parent.set(self._id)
            self._start = _perf()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        builder = self._builder
        if builder is None:
            return
        end = _perf()
        self._builder = None
        try:
            _parent.reset(self._token)
        except ValueError:  # exited in a different context than entered
            _parent.set(self._prev)
        try:
            from .capture_limits import bounded_value

            data, cut = bounded_value(
                self.data,
                max_bytes=builder.recorder.settings.max_span_data_bytes,
                max_items=200,
                max_string_bytes=builder.recorder.settings.max_sql_bytes,
            )
            if type(data) is not dict:
                data, cut = {}, True
            name, name_cut = bounded_value(
                self.name if self.name is not None else "span",
                max_bytes=2048,
                max_string_bytes=1024,
            )
            if type(name) is not str:
                name, name_cut = "[unavailable]", True
            if cut or name_cut:
                builder.capture_limited = True
                data["capture_incomplete"] = True
                if self.kind == "sql":
                    data.update(explainable=False, params_captured=False, params=None)
            if exc_type is not None:
                data["error"] = exc_type.__name__
            builder.add(
                Span(
                    id=self._id,
                    parent_id=self._prev,
                    kind=self.kind,
                    name=name,
                    start_ms=round(max(0.0, (self._start - builder.t0) * 1000.0), 3),
                    duration_ms=round((end - self._start) * 1000.0, 3),
                    callsite=None,
                    data=data,
                )
            )
        except Exception:
            builder.capture_errors += 1
            _capture_failure("Redline custom span capture failed")

    async def __aenter__(self) -> SpanContext:
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.__exit__(exc_type, exc, tb)

    @property
    def span_id(self) -> str | None:
        """The id of the open span (``None`` when nothing is being recorded)."""
        return self._id if self._builder is not None else None

    # decorator -------------------------------------------------------------
    def __call__(self, func: F) -> F:
        name = self.name or getattr(func, "__qualname__", getattr(func, "__name__", "span"))
        kind, data = self.kind, self.data

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if _trace.get() is None:
                    return await func(*args, **kwargs)
                async with SpanContext(name, kind, data):
                    return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _trace.get() is None:
                return func(*args, **kwargs)
            with SpanContext(name, kind, data):
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]


@overload
def span(name: F) -> F: ...
@overload
def span(name: str | None = None, kind: SpanKind = "custom", **data: Any) -> SpanContext: ...
def span(name: Any = None, kind: SpanKind = "custom", **data: Any) -> Any:
    """Time a block or a function as a span of the current request.

    Works as ``with span("x"):``, ``async with span("x"):``, ``@span("x")``,
    ``@span()`` (named after the function) and bare ``@span``. Extra keyword arguments
    become the span's ``data``. Outside a recorded request it does nothing.
    """
    if callable(name):
        return SpanContext(None, kind, data)(name)
    return SpanContext(name, kind, data)


# ── helpers ─────────────────────────────────────────────────────────────────────────


def _reset(builder: TraceBuilder) -> None:
    for var, token in ((_trace, builder._token), (_parent, builder._parent_token)):
        if token is None:
            continue
        try:
            var.reset(token)  # type: ignore[arg-type]
        except ValueError:  # finished from a different context (e.g. a callback)
            if var.get() is builder or var is _parent:
                var.set(None)  # type: ignore[arg-type]
    builder._token = builder._parent_token = None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return None


_TRACE_ID = re.compile(r"[0-9a-fA-F]{32}")


def _apply_run_header(builder: TraceBuilder, headers: Any) -> None:
    """Turn Redline's request headers into tags.

    ``X-Redline-Run: <run_id>;step=<n>`` (the load tester) becomes ``run_id`` / ``step``;
    ``X-Redline-Replay-Of: <trace id>`` (the UI's Replay) becomes ``replay_of`` when it is
    a 32-character hex id; ``X-Redline-Check`` (the refactor check) becomes
    ``check_run_id`` / ``check_req`` / ``check_n``. One pass over the headers either way.
    """
    if not headers:
        return
    items = headers.items() if isinstance(headers, Mapping) else headers
    for key, value in items:
        k = key.decode("latin-1") if isinstance(key, bytes) else str(key)
        k = k.lower()
        if k == "x-redline-replay-of":
            v = value.decode("latin-1") if isinstance(value, bytes) else str(value)
            if _TRACE_ID.fullmatch(v.strip()):
                builder.tags["replay_of"] = v.strip().lower()
            continue
        if k == "x-redline-check":
            _apply_check_header(builder, value)
            continue
        if k != "x-redline-run":
            continue
        v = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        run_id, _, rest = v.partition(";")
        if run_id.strip():
            builder.tags["run_id"] = run_id.strip()[:64]
        for part in rest.split(";"):
            k2, _, v2 = part.partition("=")
            if k2.strip() == "step" and v2.strip():
                builder.tags["step"] = v2.strip()[:16]


_CHECK_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CHECK_NUM = re.compile(r"-?[0-9]{1,9}")


def _apply_check_header(builder: TraceBuilder, value: Any) -> None:
    """``X-Redline-Check: <check run id>;req=<index>;n=<call#>`` (the refactor-check
    runner) → tags ``check_run_id``, ``check_req``, ``check_n``. Malformed values are
    ignored; the store keeps the tags only for a running check run (§19.2)."""
    v = value.decode("latin-1") if isinstance(value, bytes) else str(value)
    run_id, _, rest = v.partition(";")
    run_id = run_id.strip()
    if not _CHECK_ID.fullmatch(run_id):
        return
    parts = {}
    for part in rest.split(";"):
        k, _, val = part.partition("=")
        parts[k.strip().lower()] = val.strip()
    req = parts.get("req", "")
    if not _CHECK_NUM.fullmatch(req) or req.startswith("-"):
        return
    builder.tags["check_run_id"] = run_id
    builder.tags["check_req"] = req
    n = parts.get("n", "")
    if _CHECK_NUM.fullmatch(n):
        builder.tags["check_n"] = n


def _error_dict(
    error: BaseException | Mapping[str, Any] | None,
    redactor: Redactor,
    *,
    capture_details: bool = True,
) -> dict[str, Any] | None:
    """The stored ``Trace.error``: the message and the formatted traceback (chained
    exceptions included) go through :meth:`Redactor.text` – URL passwords, secret query
    values, ``key=value`` secrets, JWTs and card numbers are masked."""
    if error is None:
        return None
    if not capture_details:
        name = error.get("type", "Error") if isinstance(error, Mapping) else type(error).__name__
        return {
            "type": name[:200] if isinstance(name, str) else "Error",
            "message": "[not captured]",
            "traceback": "",
        }
    if isinstance(error, Mapping):
        return {
            str(k): redactor.text(v) if isinstance(v, str) and k != "type" else v
            for k, v in error.items()
        }
    try:
        message = str(error)
    except Exception:
        message = "<unprintable>"
    try:
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    except Exception:
        tb = ""
    if len(tb) > 40_000:  # cut at a line start so no ``key=value`` pair is split
        tb = tb[-40_000:].partition("\n")[2]
    return {
        "type": type(error).__name__,
        "message": redactor.text(message[:4000])[:2000],
        "traceback": redactor.text(tb[-40_000:])[-20_000:],
    }
