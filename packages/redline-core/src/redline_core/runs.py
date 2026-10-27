"""Load-test runs as stored by :class:`~redline_core.storage.Store` (``CONTRACT.md`` §11/§12).

The load tester (``redline_core.load``) creates a :class:`Run`, saves it with
``store.save_run(run)``, writes one :class:`RunStep` per finished (or in-progress) step with
``store.save_run_step(run_id, step)`` and ends with ``store.finish_run(run_id, status,
summary)``. The UI reads them back through ``GET api/runs`` and ``GET api/runs/{id}``.

``RunStep.hist`` is a :class:`~redline_core.histogram.Histogram` of response latencies in ms
(coordinated-omission corrected by the load tester); the percentiles in :meth:`RunStep.to_dict`
are read from it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .histogram import Histogram
from .model import new_id

__all__ = ["RUN_STATUSES", "Run", "RunStatus", "RunStep"]

RunStatus = Literal["running", "done", "stopped", "failed"]
RUN_STATUSES: tuple[str, ...] = ("running", "done", "stopped", "failed")


@dataclass
class RunStep:
    """One rate step of a run (a constant-rate run has a single step ``0``)."""

    step: int
    target_rps: float
    started_at: float = 0.0
    ended_at: float | None = None
    achieved_rps: float = 0.0
    sent: int = 0
    ok: int = 0
    errors: int = 0
    dropped: int = 0  # never sent: the per-worker in-flight cap was reached
    hist: Histogram = field(default_factory=Histogram)
    #: ``{"500": 3, "timeout": 2, "connect": 1}`` – status codes and exception kinds.
    error_breakdown: dict[str, int] = field(default_factory=dict)

    @property
    def error_rate(self) -> float:
        """Errors over completed requests (``ok + errors``); ``0`` when nothing completed."""
        done = self.ok + self.errors
        return round(self.errors / done, 4) if done else 0.0

    def to_dict(self) -> dict[str, Any]:
        """The contract's ``Step`` shape (plus ``started_at`` / ``ended_at``)."""
        h = self.hist
        return {
            "step": self.step,
            "target_rps": self.target_rps,
            "achieved_rps": round(self.achieved_rps, 3),
            "sent": self.sent,
            "ok": self.ok,
            "errors": self.errors,
            "dropped": self.dropped,
            "p50_ms": round(h.percentile(50), 3),
            "p95_ms": round(h.percentile(95), 3),
            "p99_ms": round(h.percentile(99), 3),
            "max_ms": round(h.max, 3),
            "error_rate": self.error_rate,
            "error_breakdown": dict(self.error_breakdown),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


@dataclass
class Run:
    """A load-test run: its scenario, status, steps and verdict."""

    id: str = field(default_factory=new_id)
    name: str = ""
    status: RunStatus = "running"
    #: The scenario as given (``{name, requests, profile, ...}``), JSON-safe.
    config: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    steps: list[RunStep] = field(default_factory=list)
    #: ``{"knee_step": int | None, "max_ok_rps": float | None, "verdict": str | None}``
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """``GET api/runs/{id}``: ``{id, name, status, config, created_at, finished_at,
        steps: [Step], summary: {knee_step, max_ok_rps, verdict}}``."""
        summary = {"knee_step": None, "max_ok_rps": None, "verdict": None}
        summary.update(self.summary or {})
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "config": self.config,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "steps": [s.to_dict() for s in sorted(self.steps, key=lambda s: s.step)],
            "summary": summary,
        }
