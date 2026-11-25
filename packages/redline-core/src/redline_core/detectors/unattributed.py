"""``unattributed_time``: a request spends time that no recorded span accounts for.

Fires when the part of a request not covered by any span (see :mod:`redline_core.coverage`)
is at least ``unattributed_time_ms`` (default 100 ms) *and* at least
``unattributed_time_ratio`` of the request (default 0.5). The time may be CPU work, or a
client library redline has no integration for (a data store, queue or storage client, see
:func:`redline_core.instrument_client_method`). When the trace carries a profile the finding
names the project frame that held most of the uncovered time.

Conservative by design: it looks only at requests recorded by a web adapter (Django, FastAPI,
Flask, Litestar), because a job or a hand-made recording is mostly its own code and would
always trip it. It stays silent when spans were dropped (the coverage would be wrong), when
the ratio is ``0`` (disabled) and for requests shorter than the minimum.
"""

from __future__ import annotations

import bisect
from typing import Any

from ..conf import Settings
from ..coverage import gaps
from ..model import Callsite, Frame, Issue, IssueKind, Trace
from ._common import issue_id

__all__ = ["detect"]

_KIND: IssueKind = "unattributed_time"
_UNIT_MS = {"seconds": 1000.0, "milliseconds": 1.0, "microseconds": 0.001, "nanoseconds": 1e-6}


def detect(trace: Trace, settings: Settings) -> list[Issue]:
    ratio = settings.unattributed_time_ratio
    floor = settings.unattributed_time_ms
    duration = trace.duration_ms
    if not ratio or duration <= 0 or duration < floor:
        return []
    if trace.framework == "other" or trace.method == "TASK":
        return []
    if trace.truncated and trace.truncated.get("dropped_spans"):
        return []  # spans were dropped: the coverage below would be too low
    holes = gaps(((s.kind, s.start_ms, s.duration_ms) for s in trace.spans), duration)
    uncovered = sum(hi - lo for lo, hi in holes)
    if uncovered < floor or uncovered / duration < ratio:
        return []
    callsite = _profile_callsite(trace.profile, holes, settings) if trace.profile else None
    share = uncovered / duration
    where = f" Most of it is in {callsite.function} at {callsite.location}." if callsite else ""
    return [
        Issue(
            id=issue_id(_KIND, None, trace.route or f"{trace.method} {trace.path}"),
            kind=_KIND,
            severity="warning" if uncovered >= 2 * floor else "info",
            title=f"{uncovered:.0f} ms ({share:.0%}) of the request is not attributed",
            detail=(
                f"{uncovered:.0f} ms of this {duration:.0f} ms request is not covered by any "
                "recorded span, so redline cannot say what it was spent on. It is either your own "
                "code or a client library redline has no integration for (a data store, queue "
                f"or storage client).{where} Wrap the client with "
                "redline_core.instrument_client_method, or add a span() around the code, and "
                "this time gets a name."
            ),
            callsite=callsite,
            count=1,
            total_ms=round(uncovered, 3),
            extra={
                "uncovered_ms": round(uncovered, 3),
                "uncovered_ratio": round(share, 3),
                "threshold_ms": floor,
                "ratio_threshold": ratio,
                "request_ms": round(duration, 3),
                "profiled": callsite is not None,
            },
        )
    ]


# profile hotspot ---------------------------------------------------------------------------
def _profile_callsite(
    profile: dict[str, Any], holes: list[tuple[float, float]], settings: Settings
) -> Callsite | None:
    """The project frame holding the most uncovered time, from a speedscope profile."""
    try:
        return _hotspot(profile, holes, settings)
    except Exception:  # evidence of this kind is optional; never fail the detector
        return None


def _hotspot(
    doc: dict[str, Any], holes: list[tuple[float, float]], settings: Settings
) -> Callsite | None:
    from ..callsite import CallsiteCapturer
    from ..profiler import _FRAMEWORK_MARKERS

    frames = doc.get("shared", {}).get("frames", [])
    profiles = doc.get("profiles", [])
    if not frames or not profiles or not holes:
        return None
    capturer = CallsiteCapturer(settings.project_root)
    verdicts: dict[int, str | None] = {}

    def project_file(index: int) -> str | None:
        if index not in verdicts:
            frame = frames[index] if 0 <= index < len(frames) else {}
            file = str(frame.get("file") or "")
            norm = file.replace("\\", "/")
            if any(m in norm for m in _FRAMEWORK_MARKERS):
                verdicts[index] = None
            else:
                verdicts[index] = capturer.classify(file)
        return verdicts[index]

    starts = [lo for lo, _ in holes]
    weight: dict[int, float] = {}

    def add(stack: list[int], lo: float, hi: float) -> None:
        """Credit ``[lo, hi]`` (ms from the request start) to the innermost project frame."""
        if hi <= lo or not stack:
            return
        inside = 0.0
        i = max(0, bisect.bisect_right(starts, lo) - 1)
        while i < len(holes) and holes[i][0] < hi:
            inside += max(0.0, min(hi, holes[i][1]) - max(lo, holes[i][0]))
            i += 1
        if inside <= 0:
            return
        for frame in reversed(stack):
            if project_file(frame):
                weight[frame] = weight.get(frame, 0.0) + inside
                return

    profile = profiles[0]
    scale = _UNIT_MS.get(profile.get("unit"))
    if scale is None:
        return None
    origin = float(profile.get("startValue", 0))
    if profile.get("type") == "evented":
        stack: list[int] = []
        last = origin
        for event in profile.get("events", []):
            at = float(event["at"])
            add(stack, (last - origin) * scale, (at - origin) * scale)
            last = at
            if event["type"] == "O":
                stack.append(int(event["frame"]))
            elif stack:
                stack.pop()
    elif profile.get("type") == "sampled":
        clock = 0.0
        weights = profile.get("weights") or []
        for sample, w in zip(profile.get("samples", []), weights, strict=False):
            add([int(i) for i in sample], clock * scale, (clock + float(w)) * scale)
            clock += float(w)
    else:
        return None
    if not weight:
        return None
    best = max(weight, key=lambda i: weight[i])
    frame = frames[best]
    file = project_file(best)
    if file is None:
        return None
    line = frame.get("line")
    line = line if isinstance(line, int) and line > 0 else 0
    name = str(frame.get("name") or "")
    return Callsite(
        file=file,
        line=line,
        function=name,
        code=None,
        stack=(Frame(file, line, name),),
    )
