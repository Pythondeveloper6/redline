"""How much of a request's time is covered by a recorded span.

Wrapper spans (``view``, ``middleware``, ``transaction``) enclose most of a request without
saying what the time was spent on, so they do not count as coverage. Every other span does,
including a user ``custom`` span: a named block is attributed time. Overlapping spans, such
as work on several threads, are merged so time is never counted twice.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["WRAPPER_KINDS", "gaps", "uncovered_ms"]

#: Span kinds that enclose other work instead of being the work.
WRAPPER_KINDS = frozenset({"view", "middleware", "transaction"})

Interval = tuple[float, float]


def _merged(spans: Iterable[tuple[str, float, float]], duration_ms: float) -> list[Interval]:
    """Sorted, non-overlapping ``(start, end)`` intervals inside ``[0, duration_ms]``.

    ``spans`` yields ``(kind, start_ms, duration_ms)``.
    """
    intervals = []
    for kind, start, length in spans:
        if kind in WRAPPER_KINDS:
            continue
        lo, hi = max(0.0, start), min(duration_ms, start + max(0.0, length))
        if hi > lo:
            intervals.append((lo, hi))
    intervals.sort()
    merged: list[Interval] = []
    for lo, hi in intervals:
        if merged and lo <= merged[-1][1]:
            if hi > merged[-1][1]:
                merged[-1] = (merged[-1][0], hi)
        else:
            merged.append((lo, hi))
    return merged


def gaps(spans: Iterable[tuple[str, float, float]], duration_ms: float) -> list[Interval]:
    """The intervals of ``[0, duration_ms]`` that no counted span covers."""
    if duration_ms <= 0:
        return []
    out: list[Interval] = []
    cursor = 0.0
    for lo, hi in _merged(spans, duration_ms):
        if lo > cursor:
            out.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < duration_ms:
        out.append((cursor, duration_ms))
    return out


def uncovered_ms(spans: Iterable[tuple[str, float, float]], duration_ms: float) -> float:
    """Milliseconds of the request not covered by any counted span."""
    return sum(hi - lo for lo, hi in gaps(spans, duration_ms))
