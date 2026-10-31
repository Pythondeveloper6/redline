"""Named redaction patterns and trace-wide policy, applied before persistence."""

from __future__ import annotations

import json
from fnmatch import fnmatchcase

PROFILES = {
    "credentials": {
        "keys": (r"(?i)password|secret|token|api.?key|authorization|cookie",),
        "tables": (),
    },
    "pii": {
        "keys": (
            (
                "(?i)email|phone|mobile|dob|birth|address|postcode|passport|nationa"
                "l.?id|religion|ethnicity|spouse|partner|children|next.of.kin"
            ),
        ),
        "tables": (),
    },
    "health": {
        "keys": (
            r"(?i)diagnos|injur|medical|clinical|symptom|medication|treatment|allerg|physio|notes?",
        ),
        "tables": (r"(?i)medical|injur|clinical",),
    },
    "payment": {
        "keys": (r"(?i)card|iban|cvv|account.number|(?<![a-z])pan(?![a-z])",),
        "tables": (r"(?i)payment|billing",),
    },
}


def profile_keys(names):
    return tuple(p for n in names for p in PROFILES[n]["keys"])


def profile_tables(names):
    return tuple(p for n in names for p in PROFILES[n]["tables"])


def masked_values(value):
    if isinstance(value, dict):
        return {k: masked_values(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [masked_values(v) for v in value]
    return "[redacted]"


def sanitize_trace(trace, settings):
    route = f"{trace.method} {trace.route or trace.path}"
    modes = [v for k, v in settings.redact_routes.items() if fnmatchcase(route, k)]
    if not modes:
        return
    mode = "body" if "body" in modes else "values"
    for payload in (trace.request, trace.response):
        body = payload.get("body")
        if body is not None:
            try:
                parsed = json.loads(body)
            except (ValueError, TypeError):
                parsed = None
            payload["body"] = (
                json.dumps(masked_values(parsed))
                if mode == "values" and isinstance(parsed, (dict, list))
                else "[redacted]"
            )
    trace.request["query"] = masked_values(trace.request.get("query", {}))
    from dataclasses import replace

    for span in trace.spans:
        for entry in (span.data.get("session_state_leak") or {}).values():
            if isinstance(entry.get("callsite"), dict):
                entry["callsite"]["code"] = None
        if span.callsite is not None:
            span.callsite = replace(span.callsite, code=None)
        if span.kind == "sql":
            from .sql import fingerprint

            span.data["params"] = None
            span.data["params_captured"] = False
            span.data["sql"] = fingerprint(span.data.get("sql", ""))
            span.data["explainable"] = False
            if span.data.get("error"):
                span.data["error"]["message"] = "[redacted]"
    for record in trace.logs:
        record.message = "[redacted]"
        if record.callsite is not None:
            record.callsite = replace(record.callsite, code=None)
    if trace.error:
        trace.error["message"] = "[redacted]"
        trace.error["traceback"] = "[redacted]"
        for frame in trace.error.get("frames", []):
            frame["code"] = None
            frame["locals"] = masked_values(frame.get("locals", {}))


def correlate(trace, request_headers, response_headers, settings):
    from .redact import Redactor

    def normal(headers):
        items = headers.items() if hasattr(headers, "items") else (headers or ())
        return {
            (k.decode("latin1") if isinstance(k, bytes) else str(k)).lower(): (
                v.decode("latin1") if isinstance(v, bytes) else str(v)
            )
            for k, v in items
        }

    request, response = normal(request_headers), normal(response_headers)
    from .tracecontext import parse_traceparent

    context = parse_traceparent(response.get("traceparent") or request.get("traceparent"))
    if context:
        trace.tags["otel_trace_id"] = context["trace_id"]
        trace.tags["otel_parent_span_id"] = context["parent_span_id"]
    for name in settings.request_id_headers:
        if name.lower() in {h.lower() for h in settings.redact_headers}:
            continue
        value = response.get(name.lower()) or request.get(name.lower())
        if value:
            trace.tags["request_id"] = (
                "[redacted]"
                if Redactor(settings).is_secret_key("request_id")
                else Redactor(settings).text(value[:1024])[:256]
            )
            break


def resolve_user(builder, request):
    """Resolve only an identifier, without allowing a callback error to break a response."""
    resolver = builder.recorder.settings.user_resolver
    if resolver is None:
        return
    try:
        value = resolver(request)
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            close = getattr(value, "close", None)
            if callable(close):
                close()
            raise TypeError("Expected an identifier string/integer")
        redactor = builder.recorder.redactor
        builder.tag(
            "user_id",
            "[redacted]"
            if redactor.is_secret_key("user_id")
            else redactor.text(str(value)[:1024])[:256],
        )
    except Exception as exc:
        builder.tag("user_resolver_error", type(exc).__name__)


def scrub_issue_sources(trace, settings):
    """Generated fixes can quote source literals; capture restrictions cover them too."""
    from dataclasses import replace

    route_restricted = any(
        fnmatchcase(f"{trace.method} {trace.route or trace.path}", k)
        for k in settings.redact_routes
    )
    if settings.capture_sql_params is True and not route_restricted:
        return
    for issue in trace.issues:
        if issue.callsite is not None:
            issue.callsite = replace(issue.callsite, code=None)
        if issue.suggestion is not None:
            issue.suggestion = replace(issue.suggestion, code=None, diff=None)
