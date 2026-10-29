"""Explicit OTLP/HTTP JSON export. No exporter runs on import or on the request path."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def _id(value, length=32):
    return hashlib.sha256(str(value).encode()).hexdigest()[:length]


def payload(traces):
    """Build sanitized summary spans; omit bodies, SQL text/values, errors and arbitrary tags."""
    items = []
    for trace in traces:
        start = int(trace.started_at * 1_000_000_000)
        attrs = [
            ("redline.framework", trace.framework),
            ("redline.method", trace.method),
            ("redline.status", str(trace.status)),
            ("redline.query_count", str(trace.query_count)),
        ]
        # Paths/job names and SQL identifiers can be sensitive too; hash names by default.
        items.append(
            {
                "traceId": _id(trace.id),
                "spanId": _id(trace.id + ":root", 16),
                "name": "redline.recording",
                "kind": 1,
                "startTimeUnixNano": str(start),
                "endTimeUnixNano": str(start + int(trace.duration_ms * 1_000_000)),
                "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attrs],
                "status": {"code": 2 if trace.status >= 400 else 1},
            }
        )
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "redline"}}]
                },
                "scopeSpans": [{"scope": {"name": "redline"}, "spans": items}],
            }
        ]
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def export(traces, endpoint, *, dry_run=True):
    data = payload(traces)
    if dry_run:
        return data
    parts = urlsplit(endpoint)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Use an explicit HTTP(S) collector URL without credentials or query")
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
            if result.get("partialSuccess", {}).get("rejectedSpans", 0):
                raise ValueError("Collector rejected spans")
    return {"sent": True, "recordings": len(data["resourceSpans"][0]["scopeSpans"][0]["spans"])}
