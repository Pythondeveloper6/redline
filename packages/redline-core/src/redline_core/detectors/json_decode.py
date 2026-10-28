"""Sampled JSON decoding time is evidence of cost, not proof of a safe rewrite."""

from __future__ import annotations

from ..conf import Settings
from ..model import Issue, Trace
from ._common import issue_id


def detect(trace: Trace, settings: Settings) -> list[Issue]:
    if not trace.profile or not settings.json_decode_ms:
        return []
    frames = trace.profile.get("shared", {}).get("frames", [])
    targets = {
        i
        for i, frame in enumerate(frames)
        if frame.get("name") in {"loads", "decode", "raw_decode"}
        and "/json/" in str(frame.get("file", "")).replace("\\", "/")
    }
    if not targets:
        return []
    profiles = trace.profile.get("profiles", [])
    if not profiles:
        return []
    profile = profiles[0]
    factor = {
        "seconds": 1000,
        "milliseconds": 1,
        "microseconds": 0.001,
        "nanoseconds": 0.000001,
    }.get(profile.get("unit"))
    if factor is None:
        return []
    measured = 0.0
    if profile.get("type") == "evented":
        stack = []
        last = profile.get("startValue", 0)
        for event in profile.get("events", []):
            if targets.intersection(stack):
                measured += max(0, event["at"] - last)
            last = event["at"]
            if event["type"] == "O":
                stack.append(event["frame"])
            elif stack:
                stack.pop()
    elif profile.get("type") == "sampled" and "weights" in profile:
        measured = sum(
            weight
            for sample, weight in zip(profile.get("samples", []), profile["weights"], strict=True)
            if targets.intersection(sample)
        )
    measured *= factor
    if measured < settings.json_decode_ms:
        return []
    return [
        Issue(
            id=issue_id("json_decode_hotspot", None, trace.route),
            kind="json_decode_hotspot",
            severity="warning",
            title="JSON decoding is a sampled hotspot",
            detail="The profile attributes substantial time to standard-library JSON decoding. "
            "Review repeated parsing and cached or precomputed representations. "
            "This does not prove equivalent generated-column semantics or predict savings.",
            count=1,
            total_ms=round(measured, 3),
            extra={
                "sampled_decode_ms": round(measured, 3),
                "threshold_ms": settings.json_decode_ms,
            },
        )
    ]
