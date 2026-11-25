"""Step results, knee detection and CI thresholds (``CONTRACT.md`` section 13).

The knee is the first complete step that

* has a p95 above 2× the first step's p95, or
* an error rate above 1 %, or
* achieved less than 90 % of its target rate.

``max_ok_rps`` is the achieved rate of the last step before the knee (the last step when
there is no knee; ``None`` when the very first step is the knee). Steps cut short by a stop
(``partial``) are reported but never decide the knee.

Steps where the target rate *falls* below an earlier step's (a ``stages`` profile such as a
spike: 10, 200, 10) never decide the knee: the knee is about where rising load breaks the
system. Instead :func:`recovery` reports how long p95 took to return to its level before the
spike, and :func:`drift` checks long steps for slow growth (a leak) in p95 and error rate.

Verdicts are one word, matching the UI's colouring: ``healthy`` (no knee), ``saturated``
(a knee after at least one good step), ``failing`` (the first step is already the knee),
``no data``.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..histogram import Histogram

__all__ = [
    "KNEE_ACHIEVED_RATIO",
    "KNEE_ERROR_RATE",
    "KNEE_P95_FACTOR",
    "StepResult",
    "analyze",
    "check_thresholds",
    "find_knee",
]

#: A recovered p95 is within this factor of the level before the spike, for 3 seconds running.
RECOVERY_FACTOR = 1.25
RECOVERY_HOLD_S = 3
#: A step is checked for drift when it lasted this long and has this many per-second samples.
DRIFT_MIN_STEP_S = 30.0
DRIFT_MIN_SAMPLES = 15
#: Last third vs first third: growth that makes a step ``degrading``.
DRIFT_P95_GROWTH = 1.25
DRIFT_P95_MIN_MS = 5.0
DRIFT_ERROR_GROWTH = 0.01
GENERATOR_LIMITED_CPU = 90.0
KNEE_P95_FACTOR = 2.0
KNEE_ERROR_RATE = 0.01
KNEE_ACHIEVED_RATIO = 0.9


def _r(value: float, digits: int = 3) -> float:
    return round(float(value), digits) if math.isfinite(value) else 0.0


@dataclass
class StepResult:
    """The aggregate of one step across all workers."""

    step: int
    target_rps: float
    started_at: float  # epoch seconds
    ended_at: float
    duration_s: float
    sent: int = 0  # requests actually issued (arrivals minus dropped)
    ok: int = 0
    errors: int = 0
    dropped: int = 0  # arrivals refused by the per-worker in-flight cap
    error_breakdown: dict[str, int] = field(default_factory=dict)
    hist: Histogram = field(default_factory=Histogram)  # latency from the scheduled time
    service_hist: Histogram = field(default_factory=Histogram)  # from the actual send
    partial: bool = False
    #: time to first byte (headers received) of every request; ``hist`` is the time to the
    #: last byte. For a held stream or websocket ``hist`` is the time to the first byte.
    ttfb_hist: Histogram = field(default_factory=Histogram)
    streams: dict[str, Any] = field(default_factory=dict)
    websockets: dict[str, Any] = field(default_factory=dict)
    sessions: dict[str, Any] = field(default_factory=dict)
    #: Highest CPU use of any generator worker during the step; at or above
    #: :data:`GENERATOR_LIMITED_CPU` the generator, not the app, may have capped the rate.
    generator_cpu_pct: float | None = None

    @property
    def completed(self) -> int:
        return self.ok + self.errors

    @property
    def achieved_rps(self) -> float:
        return self.completed / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def error_rate(self) -> float:
        return self.errors / self.completed if self.completed else 0.0

    def p(self, pct: float) -> float:
        return self.hist.percentile(pct) if self.hist.count else 0.0

    def to_dict(self, *, include_hist: bool = False) -> dict[str, Any]:
        """The ``Step`` shape of ``GET api/runs/{id}`` plus a few extras."""
        out: dict[str, Any] = {
            "step": self.step,
            "target_rps": _r(self.target_rps),
            "achieved_rps": _r(self.achieved_rps),
            "sent": self.sent,
            "ok": self.ok,
            "errors": self.errors,
            "dropped": self.dropped,
            "p50_ms": _r(self.p(50)),
            "p95_ms": _r(self.p(95)),
            "p99_ms": _r(self.p(99)),
            "max_ms": _r(self.hist.max if self.hist.count else 0.0),
            "mean_ms": _r(self.hist.mean if self.hist.count else 0.0),
            "error_rate": _r(self.error_rate, 5),
            "error_breakdown": dict(sorted(self.error_breakdown.items())),
            "service_p50_ms": _r(self.service_hist.percentile(50) if self.service_hist else 0.0),
            "service_p95_ms": _r(self.service_hist.percentile(95) if self.service_hist else 0.0),
            "started_at": _r(self.started_at, 3),
            "ended_at": _r(self.ended_at, 3),
            "duration_s": _r(self.duration_s),
            "partial": self.partial,
        }
        if self.ttfb_hist.count:
            out["ttfb_p50_ms"] = _r(self.ttfb_hist.percentile(50))
            out["ttfb_p95_ms"] = _r(self.ttfb_hist.percentile(95))
        if self.streams:
            out["streams"] = dict(self.streams)
        if self.websockets:
            out["websockets"] = dict(self.websockets)
        if self.sessions:
            out["sessions"] = dict(self.sessions)
        if self.generator_cpu_pct is not None:
            out["generator_cpu_pct"] = _r(self.generator_cpu_pct, 1)
            out["generator_limited"] = self.generator_cpu_pct >= GENERATOR_LIMITED_CPU
        if include_hist:
            out["hist"] = base64.b64encode(self.hist.to_bytes()).decode("ascii")
            out["service_hist"] = base64.b64encode(self.service_hist.to_bytes()).decode("ascii")
        return out


def find_knee(steps: Sequence[StepResult]) -> tuple[StepResult | None, str | None]:
    """Return ``(knee step, reason)`` or ``(None, None)``."""
    full = [s for s in steps if not s.partial and (s.completed or s.dropped)]
    if not full:
        return None, None
    base_p95 = full[0].p(95)
    peak = 0.0
    for i, s in enumerate(full):
        if s.target_rps < peak - 1e-9:
            continue  # the rate fell after a higher step: that is recovery, not a knee
        peak = max(peak, s.target_rps)
        if s.error_rate > KNEE_ERROR_RATE:
            return s, f"error rate {s.error_rate:.1%} > {KNEE_ERROR_RATE:.0%}"
        if s.target_rps > 0 and s.achieved_rps < KNEE_ACHIEVED_RATIO * s.target_rps:
            return s, (
                f"achieved {s.achieved_rps:.1f} rps < {KNEE_ACHIEVED_RATIO:.0%} "
                f"of target {s.target_rps:g}"
            )
        if i > 0 and base_p95 > 0 and s.p(95) > KNEE_P95_FACTOR * base_p95:
            return (
                s,
                f"p95 {s.p(95):.1f} ms > {KNEE_P95_FACTOR:g}× step {full[0].step}'s "
                f"{base_p95:.1f} ms",
            )
    return None, None


def _stage_seconds(seconds: Sequence[dict[str, Any]], step: int) -> list[dict[str, Any]]:
    return sorted((s for s in seconds if s.get("step") == step), key=lambda s: s["t"])


def recovery(
    steps: Sequence[StepResult],
    seconds: Sequence[dict[str, Any]],
    starts: dict[int, float],
) -> list[dict[str, Any]]:
    """After each step where the target rate fell, how long p95 took to come back.

    The reference is the p95 of the latest earlier step that ran at or below the falling
    step's rate (the level before the spike). The step recovers at the first second whose
    p95 is within :data:`RECOVERY_FACTOR` of it and stays there for
    :data:`RECOVERY_HOLD_S` seconds; ``recovered_after_s`` is measured from the start of the
    falling step and is ``None`` when it never did.
    """
    ordered = sorted(steps, key=lambda s: s.step)
    out = []
    for k in range(1, len(ordered)):
        step, prev = ordered[k], ordered[k - 1]
        if step.target_rps <= 0 or step.target_rps >= prev.target_rps - 1e-9:
            continue
        base = next(
            (
                s
                for s in reversed(ordered[:k])
                if s.target_rps <= step.target_rps + 1e-9 and not s.partial and s.completed
            ),
            None,
        )
        if base is None or base.p(95) <= 0:
            continue
        between = [s for s in ordered if base.step < s.step < step.step and s.completed]
        peak = max((s.p(95) for s in between), default=0.0)
        limit = RECOVERY_FACTOR * base.p(95)
        series = _stage_seconds(seconds, step.step)
        begin = starts.get(step.step, series[0]["t"] if series else 0.0)
        recovered: float | None = None
        for i, sec in enumerate(series):
            window = series[i : i + RECOVERY_HOLD_S]
            if all(0 < w["p95_ms"] <= limit for w in window) and (
                len(window) == RECOVERY_HOLD_S or i + len(window) == len(series)
            ):
                recovered = max(0.0, round(sec["t"] - begin, 1))
                break
        out.append(
            {
                "step": step.step,
                "target_rps": _r(step.target_rps),
                "baseline_step": base.step,
                "baseline_p95_ms": _r(base.p(95)),
                "peak_p95_ms": _r(peak),
                "recovered_after_s": recovered,
                "seconds_observed": len(series),
            }
        )
    return out


def _slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den if den else 0.0


def drift(steps: Sequence[StepResult], seconds: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Slope of p95 and of the error rate over each long step: ``stable`` or ``degrading``.

    A step is ``degrading`` when its last third's p95 is at least :data:`DRIFT_P95_GROWTH`
    times the first third's (and ``DRIFT_P95_MIN_MS`` higher) with a positive slope, or its
    error rate grew by :data:`DRIFT_ERROR_GROWTH` or more.
    """
    out = []
    for step in sorted(steps, key=lambda s: s.step):
        if step.partial or step.duration_s < DRIFT_MIN_STEP_S:
            continue
        series = [s for s in _stage_seconds(seconds, step.step) if s["ok"] + s["errors"] > 0]
        if len(series) < DRIFT_MIN_SAMPLES:
            continue
        xs = [s["t"] - series[0]["t"] for s in series]
        p95 = [float(s["p95_ms"]) for s in series]
        err = [s["errors"] / (s["ok"] + s["errors"]) for s in series]
        third = max(1, len(series) // 3)
        first_p95, last_p95 = sum(p95[:third]) / third, sum(p95[-third:]) / third
        first_err, last_err = sum(err[:third]) / third, sum(err[-third:]) / third
        p95_slope = _slope(xs, p95) * 60.0
        err_slope = _slope(xs, err) * 60.0
        degrading = (
            p95_slope > 0
            and first_p95 > 0
            and last_p95 >= DRIFT_P95_GROWTH * first_p95
            and last_p95 - first_p95 >= DRIFT_P95_MIN_MS
        ) or (last_err - first_err >= DRIFT_ERROR_GROWTH and err_slope > 0)
        out.append(
            {
                "step": step.step,
                "target_rps": _r(step.target_rps),
                "duration_s": _r(step.duration_s),
                "first_third_p95_ms": _r(first_p95),
                "last_third_p95_ms": _r(last_p95),
                "p95_slope_ms_per_min": _r(p95_slope),
                "error_rate_slope_per_min": _r(err_slope, 5),
                "verdict": "degrading" if degrading else "stable",
            }
        )
    return out


def analyze(
    steps: Sequence[StepResult],
    seconds: Sequence[dict[str, Any]] | None = None,
    starts: dict[int, float] | None = None,
) -> dict[str, Any]:
    """The run summary: ``knee_step``, ``max_ok_rps``, ``verdict`` and run-wide totals.

    With the per-``seconds`` series (and each step's ``starts`` offset) it also reports
    ``recovery`` and ``drift`` when there is anything to say about them.
    """
    knee, reason = find_knee(steps)
    full = [s for s in steps if not s.partial and (s.completed or s.dropped)]
    max_ok: float | None
    if not full:
        verdict, max_ok = "no data", None
    elif knee is None:
        verdict, max_ok = "healthy", full[-1].achieved_rps
    else:
        before = [s for s in full if s.step < knee.step]
        max_ok = before[-1].achieved_rps if before else None
        verdict = "saturated" if before else "failing"
    total = Histogram()
    for s in steps:
        total.merge(s.hist)
    ok = sum(s.ok for s in steps)
    errors = sum(s.errors for s in steps)
    if not full:
        detail = "no complete step"
    elif knee is None:
        detail = f"no knee up to {full[-1].target_rps:g} rps"
    else:
        detail = f"knee at step {knee.step} ({knee.target_rps:g} rps): {reason}"
    extra: dict[str, Any] = {}
    ttfb = Histogram()
    for s in steps:
        ttfb.merge(s.ttfb_hist)
    if ttfb.count:
        extra["ttfb_p50_ms"] = _r(ttfb.percentile(50))
        extra["ttfb_p95_ms"] = _r(ttfb.percentile(95))
    for key in ("streams", "websockets"):
        parts = [s.__dict__[key] for s in steps if s.__dict__[key]]
        if parts:
            totals: dict[str, Any] = {
                "count": sum(p.get("count", 0) for p in parts),
                "max_open": max(p.get("max_open", 0) for p in parts),
                "by_step": {s.step: dict(s.__dict__[key]) for s in steps if s.__dict__[key]},
            }
            if key == "streams":
                totals["events"] = sum(p.get("events", 0) for p in parts)
            else:
                totals["messages"] = sum(p.get("messages", 0) for p in parts)
            extra[key] = totals
    cpus = {s.step: s.generator_cpu_pct for s in steps if s.generator_cpu_pct is not None}
    if cpus:
        extra["generator"] = {
            "max_cpu_pct": _r(max(cpus.values()), 1),
            "limited_steps": sorted(k for k, v in cpus.items() if v >= GENERATOR_LIMITED_CPU),
        }
    held = [s for s in steps if s.sessions]
    if held:
        extra["sessions"] = {
            "max_open": max(s.sessions["max_open"] for s in held),
            "by_step": {s.step: dict(s.sessions) for s in held},
        }
    if seconds:
        rec = recovery(steps, seconds, starts or {})
        if rec:
            extra["recovery"] = rec
        dr = drift(steps, seconds)
        if dr:
            extra["drift"] = dr
            extra["drift_verdict"] = (
                "degrading" if any(d["verdict"] == "degrading" for d in dr) else "stable"
            )
    return {
        **extra,
        "knee_step": knee.step if knee else None,
        "max_ok_rps": _r(max_ok) if max_ok is not None else None,
        "verdict": verdict,
        "verdict_detail": detail,
        "knee_reason": reason,
        "sent": sum(s.sent for s in steps),
        "ok": ok,
        "errors": errors,
        "dropped": sum(s.dropped for s in steps),
        "error_rate": _r(errors / (ok + errors), 5) if ok + errors else 0.0,
        "p50_ms": _r(total.percentile(50)) if total.count else 0.0,
        "p95_ms": _r(total.percentile(95)) if total.count else 0.0,
        "p99_ms": _r(total.percentile(99)) if total.count else 0.0,
        "max_ms": _r(total.max) if total.count else 0.0,
    }


def check_thresholds(
    summary: dict[str, Any],
    *,
    fail_p95_ms: float | None = None,
    fail_error_rate: float | None = None,
) -> list[str]:
    """Breached ``--fail-*`` thresholds (run-wide p95 / error rate), as messages."""
    breaches = []
    if fail_p95_ms is not None and summary.get("p95_ms", 0.0) > fail_p95_ms:
        breaches.append(f"p95 {summary['p95_ms']:.1f} ms > {fail_p95_ms:g} ms")
    if fail_error_rate is not None and summary.get("error_rate", 0.0) > fail_error_rate:
        breaches.append(f"error rate {summary['error_rate']:.2%} > {fail_error_rate:.2%}")
    return breaches
