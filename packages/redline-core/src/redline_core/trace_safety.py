"""Final fail-closed trace capture limits, independent of application object hooks."""

from __future__ import annotations

import dataclasses
import json
import math
import re
from typing import Any

from .capture_limits import bounded_value
from .model import Callsite, Frame, Issue, LogRecord, Span, Suggestion, Trace, new_id

_MODELS = (Trace, Span, Issue, LogRecord, Callsite, Frame, Suggestion)


def _number(value, *, integer=False):
    if type(value) not in (int, float):
        return 0
    if type(value) is int and value.bit_length() > 63:
        return 0
    if type(value) is float and not math.isfinite(value):
        return 0
    return int(value) if integer else value


def minimal_trace(trace: Trace, reason: str) -> Trace:
    """Return fresh metadata only; no application paths, labels, tags or free text.

    Failure counts are retained in ``truncated`` rather than application tags. Only
    fixed reason codes are allowed, so an exception string cannot become metadata.
    """
    source = trace if type(trace) is Trace else Trace(new_id(), "other", "GET", "")
    identity = source.id
    if type(identity) is not str or not re.fullmatch(r"[0-9a-fA-F]{32}", identity):
        identity = new_id()
    code = (
        reason
        if type(reason) is str
        and reason in {"privacy_failure", "capture_failure", "trace_budget", "invalid_capture"}
        else "capture_failure"
    )
    markers: dict[str, Any] = {"incomplete_evidence": True, "capture_limits": True, "reason": code}
    if type(source.tags) is dict:
        for key in ("sql_errors", "connection_errors"):
            value = source.tags.get(key)
            if type(value) is str and len(value) <= 18 and value.isascii() and value.isdigit():
                markers[key] = int(value)
            elif type(value) is int and 0 <= value < 2**63:
                markers[key] = value
    error = None
    if type(source.error) is dict:
        kind = source.error.get("type")
        if type(kind) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", kind):
            error = {"type": kind}
    return Trace(
        id=identity,
        framework=source.framework
        if type(source.framework) is str
        and source.framework in {"django", "flask", "fastapi", "litestar", "other"}
        else "other",
        method=source.method
        if type(source.method) is str
        and source.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TASK"}
        else "GET",
        path="/[capture-unavailable]",
        status=_number(source.status, integer=True),
        started_at=_number(source.started_at),
        duration_ms=_number(source.duration_ms),
        query_count=_number(source.query_count, integer=True),
        query_ms=_number(source.query_ms),
        error=error,
        logs_dropped=_number(source.logs_dropped, integer=True)
        + (len(source.logs) if type(source.logs) is list else 0),
        truncated=markers,
    )


def _size(trace):
    # Match the store's default separators, include the profile conservatively, and
    # reject NaN/custom values instead of invoking a default conversion function.
    return max(
        len(
            json.dumps(
                trace.to_dict(include_profile=include), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
        )
        for include in (True, False)
    )


def enforce_trace_limits(trace: Trace, settings: Any) -> Trace | None:
    """Return a detached, JSON-safe, budgeted recording, or refuse capture entirely.

    ``truncated.capture_limits`` and ``incomplete_evidence`` are true whenever this
    layer omits/replaces data. ``reason=trace_budget`` identifies total-size reduction;
    ``reason=invalid_capture`` identifies a malformed model fallback. Existing span-cap
    markers survive. A minimal fallback retains failure counts under ``truncated``.
    No exception path returns the original recording.
    """
    limit = settings.max_trace_bytes
    try:
        if type(trace) is not Trace:
            raise ValueError("invalid trace model")
        changed = False
        item_limit = min(4000, max(1, limit // 64))
        work_bytes = 0
        last_cut = False

        def clean(value, budget, *, string_limit=2000, items=1000):
            nonlocal changed, work_bytes, last_cut
            result, cut = bounded_value(
                value,
                max_bytes=max(4, budget),
                max_items=items,
                max_string_bytes=min(string_limit, max(0, budget)),
            )
            changed |= cut
            last_cut = cut
            work_bytes += len(
                json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
            return result

        def model(value, depth=0):
            nonlocal changed
            if type(value) not in _MODELS or depth > 12:
                changed = True
                return None
            output = {}
            span_incomplete = False
            for field in dataclasses.fields(value):
                child = getattr(value, field.name)
                if field.name == "tags" and type(value) is Trace:
                    tags = {}
                    if type(child) is dict:
                        for n, (key, tag) in enumerate(child.items()):
                            if n >= 100:
                                changed = True
                                break
                            if type(key) is not str or len(key) > 100:
                                changed = True
                                continue
                            safe = clean(
                                tag,
                                settings.max_tag_bytes,
                                string_limit=settings.max_tag_bytes,
                                items=100,
                            )
                            # Model tag contract is str; do not stringify structured values.
                            if type(safe) is str:
                                safe_key = clean(key, 602, string_limit=100, items=1)
                                if safe_key in tags:
                                    changed = True
                                else:
                                    tags[safe_key] = safe
                            else:
                                changed = True
                    else:
                        changed = True
                    output[field.name] = tags
                elif type(child) in _MODELS:
                    output[field.name] = model(child, depth + 1)
                elif field.name in {"spans", "issues", "logs", "stack"}:
                    result = []
                    if type(child) in (list, tuple):
                        for index, entry in enumerate(child):
                            if index >= item_limit or work_bytes > limit * 2:
                                changed = True
                                break
                            safe = model(entry, depth + 1)
                            if safe is not None:
                                result.append(safe)
                    else:
                        changed = True
                    output[field.name] = result
                else:
                    budget = (
                        settings.max_span_data_bytes
                        if field.name == "data" and type(value) is Span
                        else min(limit, 65536)
                    )
                    if field.name == "profile":
                        budget = min(limit, 262144)
                    # Source/error/body metadata gets bounded traversal too, without
                    # invoking arbitrary mapping, repr or sequence hooks.
                    output[field.name] = clean(child, budget, string_limit=budget)
                    if type(value) is Span and field.name in {"data", "name"} and last_cut:
                        span_incomplete = True
            if type(value) is Span and span_incomplete:
                safe_data = output.get("data")
                safe_data = safe_data if type(safe_data) is dict else {}
                flags = {"capture_incomplete": True}
                if value.kind == "sql":
                    flags.update(explainable=False, params_captured=False, params=None)
                flags.update({key: entry for key, entry in safe_data.items() if key not in flags})
                output["data"] = clean(
                    flags, settings.max_span_data_bytes, string_limit=settings.max_span_data_bytes
                )
            return output

        result = Trace.from_dict(model(trace))
        # Reserved failure counters may occur beyond the ordinary tag item budget.
        if type(trace.tags) is dict:
            for key in ("sql_errors", "connection_errors"):
                value = trace.tags.get(key)
                if type(value) is str and len(value) <= 18 and value.isascii() and value.isdigit():
                    result.tags[key] = value
        if type(trace.logs) is list:
            result.logs_dropped += max(0, len(trace.logs) - len(result.logs))
        if changed:
            result.truncated = dict(result.truncated or {})
            result.truncated.update(capture_limits=True, incomplete_evidence=True)
            if type(trace.spans) is list and len(trace.spans) > len(result.spans):
                result.truncated["budget_dropped_spans"] = len(trace.spans) - len(result.spans)
            if type(trace.issues) is list and len(trace.issues) > len(result.issues):
                result.truncated["budget_dropped_issues"] = len(trace.issues) - len(result.issues)
        if _size(result) <= limit:
            return result
        result.truncated = dict(result.truncated or {})
        result.truncated.update(
            capture_limits=True, incomplete_evidence=True, reason="trace_budget"
        )
        result.profile = None
        result.logs_dropped += len(result.logs)
        result.logs = []
        result.request = {
            "headers": {},
            "query": {},
            "body": None,
            "body_truncated": True,
            "client": None,
        }
        result.response = {"headers": {}, "body": None, "body_truncated": True, "size": None}
        result.error = (
            {"type": result.error.get("type", "Error")} if type(result.error) is dict else None
        )
        result.issues = []
        for span in result.spans:
            span.callsite = None
        while result.spans and _size(result) > limit:
            before = len(result.spans)
            result.spans = result.spans[: before // 2]
            result.truncated["budget_dropped_spans"] = (
                result.truncated.get("budget_dropped_spans", 0) + before - len(result.spans)
            )
        if _size(result) <= limit:
            return result
        result = minimal_trace(trace, "trace_budget")
    except Exception:
        result = minimal_trace(trace, "invalid_capture")
    try:
        return result if _size(result) <= limit else None
    except Exception:
        return None
