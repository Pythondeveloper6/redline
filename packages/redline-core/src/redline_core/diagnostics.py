"""Observation quality and overlap-aware time attribution."""

from __future__ import annotations

import logging
import math
import sys
from collections import Counter

from .model import Trace


def trace_diagnostics(trace: Trace) -> dict:
    """Attribute intervals to a single category or explicit overlap, never twice."""
    total = trace.duration_ms
    total = max(0.0, total) if math.isfinite(total) else 0.0
    names = {"sql": "database", "http_out": "external", "pool_wait": "pool_wait"}
    events: dict[float, list[tuple[str, int]]] = {0.0: [], total: []}
    for span in trace.spans:
        if span.kind not in names or not all(
            math.isfinite(v) for v in (span.start_ms, span.duration_ms)
        ):
            continue
        start = max(0.0, min(total, span.start_ms))
        end = max(start, min(total, span.start_ms + max(0, span.duration_ms)))
        if end > start:
            events.setdefault(start, []).append((names[span.kind], 1))
            events.setdefault(end, []).append((names[span.kind], -1))
    active: Counter = Counter()
    times = dict.fromkeys([*names.values(), "overlap", "remaining"], 0.0)
    previous = 0.0
    for t, changes in sorted(events.items()):
        categories = [key for key, count in active.items() if count > 0]
        category = (
            "overlap" if len(categories) > 1 else categories[0] if categories else "remaining"
        )
        times[category] += t - previous
        for key, delta in changes:
            active[key] += delta
        previous = t
    counts = dict(Counter(s.kind for s in trace.spans))
    warnings = []
    if trace.tags.get("synthetic_demo"):
        warnings.append(
            "Synthetic sample: illustrative operations and timings, not measured traffic."
        )
    if trace.truncated:
        warnings.append(
            "Some spans were dropped; the time breakdown and issue coverage are incomplete."
        )
    if trace.logs_dropped:
        warnings.append(f"{trace.logs_dropped} log records were dropped.")
    if trace.tags.get("profile_error"):
        warnings.append("Profiling failed for this request.")
    return {
        "duration_ms": total,
        "time_ms": {k: round(v, 6) for k, v in times.items()},
        "observed_span_kinds": counts,
        "profile_available": trace.has_profile,
        "complete_spans": not bool(trace.truncated),
        "warnings": warnings,
        "note": "Remaining time is uninstrumented time, not measured CPU time. "
        "Overlap contains time with multiple instrumented categories active.",
    }


def capture_failure(message: str, *args) -> None:
    """Log a fixed operation label and exception type, without exception values."""
    failure = sys.exc_info()[0]
    try:
        logging.getLogger("redline").error(
            message + " (%s)", *args, failure.__name__ if failure else "Error"
        )
    except Exception:
        # Application handlers can themselves fail; observation stays fail-open.
        pass
