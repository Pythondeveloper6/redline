"""W3C trace-context parsing and stable OTLP identifiers; no request body is read."""

from __future__ import annotations

import hashlib
import re

_TRACEPARENT = re.compile(
    r"^(?P<version>[0-9a-fA-F]{2})-(?P<trace>[0-9a-fA-F]{32})-"
    r"(?P<span>[0-9a-fA-F]{16})-(?P<flags>[0-9a-fA-F]{2})(?:-.*)?$"
)


def hex_id(value, length=32):
    """A stable lowercase hex identifier derived from a non-secret recording id."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:length]


def parse_traceparent(value):
    """Validate a W3C ``traceparent`` value; return ids or ``None``.

    Version ``ff`` is invalid, and the all-zero trace/span ids are reserved.
    """
    if not isinstance(value, str):
        return None
    match = _TRACEPARENT.fullmatch(value.strip())
    if not match or match["version"].lower() == "ff":
        return None
    trace_id, span_id = match["trace"].lower(), match["span"].lower()
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return {
        "trace_id": trace_id,
        "parent_span_id": span_id,
        "sampled": bool(int(match["flags"], 16) & 1),
    }


def trace_id(trace):
    """Use the incoming W3C trace id when present, else derive one from the recording."""
    value = trace.tags.get("otel_trace_id")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{32}", value):
        if value.lower() != "0" * 32:
            return value.lower()
    return hex_id("redline:" + trace.id)


def parent_span_id(trace):
    value = trace.tags.get("otel_parent_span_id")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{16}", value):
        if value.lower() != "0" * 16:
            return value.lower()
    return None


def outgoing_traceparent(trace):
    """A child ``traceparent`` for a recorded parent context."""
    return f"00-{trace_id(trace)}-{hex_id('redline:' + trace.id + ':span', 16)}-01"
