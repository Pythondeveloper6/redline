"""``slow_serializer``: a ``serializer`` span (DRF, Pydantic, Marshmallow) over its budget.

*warning* at ``>= slow_serializer_ms``, *critical* at ``>= 5x``. Spans of the same
serializer class from the same callsite are one issue (``count`` runs, ``total_ms``).
"""

from __future__ import annotations

from ..conf import Settings
from ..model import Issue, Severity, Span, Trace
from ._common import issue_id, where

__all__ = ["detect"]


def detect(trace: Trace, settings: Settings) -> list[Issue]:
    limit = settings.slow_serializer_ms
    groups: dict[tuple[str, str, str | None], list[Span]] = {}
    for span in trace.spans:
        if span.kind != "serializer" or span.duration_ms < limit:
            continue
        data = span.data
        name = str(data.get("serializer") or span.name)
        loc = span.callsite.location if span.callsite else None
        groups.setdefault((str(data.get("library") or "other"), name, loc), []).append(span)
    issues: list[Issue] = []
    for (library, name, loc), group in groups.items():
        slowest = max(group, key=lambda s: s.duration_ms)
        worst = slowest.duration_ms
        items = slowest.data.get("items")
        severity: Severity = "critical" if worst >= 5 * limit else "warning"
        of = f" ({items:,} items)" if isinstance(items, int) else ""
        callsite = slowest.callsite
        issues.append(
            Issue(
                id=issue_id("slow_serializer", f"{library}:{name}", loc or trace.route),
                kind="slow_serializer",
                severity=severity,
                title=f"Slow serializer: {name} took {worst:.0f} ms{of}{where(callsite)}",
                detail=(
                    f"{name} spent {worst:.1f} ms turning data into the response, over the "
                    f"{limit:g} ms budget. Look for per-item queries, expensive computed "
                    "fields and nested serializers; trim the fields or paginate."
                ),
                span_ids=[s.id for s in group],
                fingerprint=f"{library}:{name}",
                callsite=callsite,
                count=len(group),
                total_ms=round(sum(s.duration_ms for s in group), 3),
                extra={
                    "serializer": name,
                    "library": library,
                    "items": items if isinstance(items, int) else None,
                    "many": bool(slowest.data.get("many")),
                    "max_ms": round(worst, 3),
                    "threshold_ms": limit,
                },
            )
        )
    return issues
