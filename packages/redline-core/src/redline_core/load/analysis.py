"""Step results, knee detection and CI thresholds (``CONTRACT.md`` section 13).

The knee is the first complete step that

* has a p95 above 2× the first step's p95, or
* an error rate above 1 %, or
* achieved less than 90 % of its target rate.

``max_ok_rps`` is the achieved rate of the last step before the knee (the last step when
there is no knee; ``None`` when the very first step is the knee). Steps cut short by a stop
(``partial``) are reported but never decide the knee.

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
    for i, s in enumerate(full):
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


def analyze(steps: Sequence[StepResult]) -> dict[str, Any]:
    """The run summary: ``knee_step``, ``max_ok_rps``, ``verdict`` and run-wide totals."""
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
    return {
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
