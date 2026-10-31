"""Explicit OTLP/HTTP JSON export. No exporter runs on import or on the request path."""

from __future__ import annotations

import json
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .tracecontext import hex_id, parent_span_id, trace_id


def _value(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    return {"stringValue": str(value)}


def _attributes(items):
    return [{"key": key, "value": _value(value)} for key, value in items]


def _status(trace):
    return {"code": 2 if trace.status >= 400 else 1}


def _root_span(trace, root_id, otel_trace_id):
    start = int(trace.started_at * 1_000_000_000)
    span = {
        "traceId": otel_trace_id,
        "spanId": root_id,
        "name": "redline.recording",
        "kind": 1,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + int(trace.duration_ms * 1_000_000)),
        "attributes": _attributes(
            [
                ("redline.trace_id", trace.id),
                ("redline.framework", trace.framework),
                ("redline.method", trace.method),
                ("redline.status", str(trace.status)),
                ("redline.query_count", str(trace.query_count)),
                ("redline.incomplete", bool(trace.truncated)),
            ]
        ),
        "status": _status(trace),
    }
    incoming_parent = parent_span_id(trace)
    if incoming_parent:
        span["parentSpanId"] = incoming_parent
    return span


def _sql_span(trace, span, root_id, otel_trace_id, include_sql, redactor=None):
    error = span.data.get("error") or {}
    kinds = span.data.get("statement_kinds") or ()
    operation = str(kinds[0]).upper() if kinds else "SQL"
    statement = span.data.get("sql") if include_sql else span.data.get("fingerprint")
    attributes = [
        ("db.system", span.data.get("vendor") or "other"),
        ("db.operation", operation),
        ("redline.trace_id", trace.id),
        ("redline.db_alias", span.data.get("db_alias") or "default"),
        ("redline.rows", str(span.data.get("rows", ""))),
    ]
    if statement:
        statement = str(statement)
        if include_sql and redactor is not None:
            statement = redactor.text(statement)
        attributes.append(("db.statement", statement))
    if error:
        attributes.append(
            ("error.type", str(error.get("type") or error.get("category") or "error"))
        )
    start = int(trace.started_at * 1_000_000_000) + int(span.start_ms * 1_000_000)
    parent = hex_id("redline:" + trace.id + ":" + (span.parent_id or "root"), 16)
    result = {
        "traceId": otel_trace_id,
        "spanId": hex_id("redline:" + trace.id + ":" + span.id, 16),
        "parentSpanId": parent if span.parent_id else root_id,
        "name": operation,
        "kind": 3,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + int(span.duration_ms * 1_000_000)),
        "attributes": _attributes(attributes),
        "status": {"code": 2 if error else 1},
    }
    if error:
        result["status"]["message"] = "SQL statement failed"
    return result


def payload(traces, *, include_sql=False, redactor=None):
    """OTLP traces with a root span plus sanitized SQL spans.

    SQL text is the normalized fingerprint by default; bodies, parameter values, arbitrary
    tags and error messages are never exported.
    """
    spans = []
    for trace in traces:
        otel_trace_id = trace_id(trace)
        root_id = hex_id("redline:" + trace.id + ":root", 16)
        spans.append(_root_span(trace, root_id, otel_trace_id))
        for span in trace.spans:
            if span.kind == "sql":
                spans.append(_sql_span(trace, span, root_id, otel_trace_id, include_sql, redactor))
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "redline"}}]
                },
                "scopeSpans": [{"scope": {"name": "redline"}, "spans": spans}],
            }
        ]
    }


def _title(title, redactor):
    text = str(title)[:4000]
    return (redactor.text(text) if redactor is not None else text)[:512]


_SEVERITY = {"info": (9, "INFO"), "warning": (13, "WARN"), "critical": (17, "ERROR")}


def logs_payload(traces, *, redactor=None):
    """OTLP log records for recorded issues; stable ids make repeated exports deduplicable."""
    records = []
    for trace in traces:
        otel_trace_id = trace_id(trace)
        root_id = hex_id("redline:" + trace.id + ":root", 16)
        stamp = str(int(trace.started_at * 1_000_000_000))
        for issue in trace.issues:
            number, text = _SEVERITY.get(issue.severity, (9, "INFO"))
            records.append(
                {
                    "timeUnixNano": stamp,
                    "severityNumber": number,
                    "severityText": text,
                    "body": {"stringValue": _title(issue.title, redactor)},
                    "attributes": _attributes(
                        [
                            ("issue.id", issue.id),
                            ("issue.kind", issue.kind),
                            ("issue.severity", issue.severity),
                            ("issue.count", str(issue.count)),
                            ("redline.trace_id", trace.id),
                        ]
                    ),
                    "traceId": otel_trace_id,
                    "spanId": root_id,
                }
            )
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "redline"}}]
                },
                "scopeLogs": [{"scope": {"name": "redline"}, "logRecords": records}],
            }
        ]
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(value):
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Use an explicit HTTP(S) collector URL without credentials or query")


def _send(endpoint, data):
    _endpoint(endpoint)
    request = Request(
        endpoint,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with build_opener(_NoRedirect).open(request, timeout=10) as response:
        body = response.read(65537)
        if len(body) > 65536:
            raise ValueError("Collector response exceeds limit")
        if body:
            result = json.loads(body)
            rejected = result.get("partialSuccess", {})
            if any(
                rejected.get(key, 0) for key in ("rejectedSpans", "rejectedLogRecords", "rejected")
            ):
                raise ValueError("Collector rejected telemetry")


def export(traces, endpoint, *, dry_run=True, logs_endpoint=None, include_sql=False):
    """Preview, or explicitly POST spans (plus logs when requested) to an OTLP collector.

    Issue titles and (with ``include_sql``) raw SQL go through the project's redactor, as
    in the HTML export and the digest.
    """
    from .report_html import project_redactor

    redactor = project_redactor()
    spans = payload(traces, include_sql=include_sql, redactor=redactor)
    logs = logs_payload(traces, redactor=redactor)
    if dry_run:
        return {"spans": spans, "logs": logs}
    if not endpoint:
        raise ValueError("An explicit traces endpoint is required for --send")
    _send(endpoint, spans)
    log_records = len(logs["resourceLogs"][0]["scopeLogs"][0]["logRecords"])
    if logs_endpoint and log_records:
        _send(logs_endpoint, logs)
    return {
        "sent": True,
        "recordings": len(traces),
        "spans": len(spans["resourceSpans"][0]["scopeSpans"][0]["spans"]),
        "log_records": log_records,
        "logs_sent": bool(logs_endpoint and log_records),
    }
