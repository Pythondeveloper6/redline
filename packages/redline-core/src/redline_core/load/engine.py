"""The parent side of a load run: spawn workers, align them, aggregate, decide.

Process model
    ``N`` worker processes (``multiprocessing`` *spawn* context; default
    ``min(os.cpu_count(), 8)``, never more than the peak target rate) each run one asyncio
    loop with one ``httpx.AsyncClient`` (:mod:`redline_core.load.worker`). Each worker sends
    ``1/N`` of every step's target rate.

Clocking
    The step plan is computed once, up front, and shipped to every worker. When all
    workers report ``ready`` the parent publishes one shared start timestamp (wall-clock
    epoch, ~0.25 s in the future) and sets the ``go`` event. Each worker converts that
    epoch to its own ``time.perf_counter`` timeline once and then follows the plan with
    monotonic arithmetic only, so step boundaries are aligned across processes (to within
    clock-read jitter) without any per-step messaging; fixed-interval arrivals are
    phase-shifted per worker so their union is exactly one request every ``1/rate``.
    The parent never drives step changes – it only ever *stops* the workers (shared stop
    event): ``stop_on``, ``stop_event``, Ctrl-C or a failure.

Aggregation
    Workers stream per-second deltas (counters + :class:`~redline_core.histogram.Histogram`
    bytes) keyed by step. A request belongs to the step it was *scheduled* in (the same
    ``step=<n>`` it carried in ``X-Redline-Run``), even when it completes later. A step is
    final once every worker sent ``step_done`` for it (or exited); it is then handed to the
    sink and checked against ``stop_on``.

Statuses: ``done`` (completed, also when ``stop_on`` ended it early – see
``summary["stopped_reason"]``), ``stopped`` (Ctrl-C / ``stop_event``; partial results are
kept) and ``failed`` (a worker crashed or could not start; ``summary["message"]``).
"""

from __future__ import annotations

import importlib.util
import math
import multiprocessing as mp
import os
import queue as queue_mod
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..histogram import Histogram
from .analysis import StepResult, analyze
from .profile import Profile, StepPlan, parse_profile
from .scenario import Scenario, ScenarioError, load_scenario, validate_base_url
from .sinks import MultiSink, RunMeta, RunSink, redact_config

__all__ = ["RunResult", "default_workers", "run"]

READY_TIMEOUT_S = 60.0
START_DELAY_S = 0.25
EXIT_GRACE_S = 5.0

EventCallback = Callable[[str, Any], None]


def default_workers() -> int:
    return max(1, min(os.cpu_count() or 1, 8))


@dataclass
class RunResult:
    run_id: str
    name: str
    status: str  # done | stopped | failed
    config: dict[str, Any]
    created_at: float
    finished_at: float
    steps: list[StepResult] = field(default_factory=list)
    seconds: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    message: str | None = None

    @property
    def knee_step(self) -> int | None:
        return self.summary.get("knee_step")

    @property
    def max_ok_rps(self) -> float | None:
        return self.summary.get("max_ok_rps")

    def to_dict(self, *, include_hist: bool = False) -> dict[str, Any]:
        return {
            "id": self.run_id,
            "name": self.name,
            "status": self.status,
            "config": self.config,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "steps": [s.to_dict(include_hist=include_hist) for s in self.steps],
            "seconds": self.seconds,
            "summary": self.summary,
            "message": self.message,
        }


class _StepAgg:
    def __init__(self, plan: StepPlan) -> None:
        self.plan = plan
        self.sent = self.ok = self.errors = self.dropped = 0
        self.breakdown: Counter[str] = Counter()
        self.hist = Histogram()
        self.svc = Histogram()
        self.done: dict[int, tuple[float, bool]] = {}  # worker -> (until, partial)
        self.final = False

    def completed_any(self) -> bool:
        return bool(self.ok or self.errors)

    def add(self, m: Mapping[str, Any]) -> None:
        self.sent += m["sent"]
        self.ok += m["ok"]
        self.errors += m["errors"]
        self.dropped += m["dropped"]
        self.breakdown.update(m["breakdown"])
        if m["hist"]:
            self.hist.merge(Histogram.from_bytes(m["hist"]))
        if m["svc"]:
            self.svc.merge(Histogram.from_bytes(m["svc"]))


class _Window:
    """Per-second live numbers (merged across steps and workers)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.sent = self.ok = self.errors = self.dropped = 0
        self.hist = Histogram()

    def add(self, m: Mapping[str, Any]) -> None:
        self.sent += m["sent"]
        self.ok += m["ok"]
        self.errors += m["errors"]
        self.dropped += m["dropped"]
        if m["hist"]:
            self.hist.merge(Histogram.from_bytes(m["hist"]))


def _coerce_profile(profile: Profile | Mapping[str, Any] | None, scenario: Scenario) -> Profile:
    if profile is None:
        return scenario.profile
    if isinstance(profile, Profile):
        return profile
    return parse_profile(profile, "profile")


def run(
    scenario: Scenario | Mapping[str, Any] | str | os.PathLike[str],
    profile: Profile | Mapping[str, Any] | None = None,
    sink: RunSink | Iterable[RunSink] | None = None,
    stop_event: Any = None,
    *,
    base_url: str | None = None,
    workers: int | None = None,
    run_id: str | None = None,
    name: str | None = None,
    on_event: EventCallback | None = None,
) -> RunResult:
    """Run a load test and return its :class:`RunResult` (blocking).

    ``stop_event`` is anything with ``is_set()`` (``threading.Event``, a multiprocessing
    event, :class:`~redline_core.load.control.StopFile`); setting it stops the run
    gracefully with status ``stopped``. ``on_event(kind, data)`` receives live events:
    ``("start", RunMeta)``, ``("second", dict)``, ``("step", StepResult)``,
    ``("stopping", reason)``.

    Workers are started with the *spawn* method, so a script calling this must guard its
    entry point with ``if __name__ == "__main__":``.
    """
    sc = load_scenario(scenario)
    if base_url is not None:
        sc = sc.replace(base_url=validate_base_url(base_url, "--base-url"))
    if not sc.base_url:
        raise ScenarioError("is required (in the scenario or via --base-url)", "base_url")
    prof = _coerce_profile(profile, sc)
    sc = sc.replace(profile=prof)
    if importlib.util.find_spec("httpx") is None:
        raise RuntimeError("the load tester needs httpx: pip install 'redline-core[load]'")

    plans = prof.plan()
    n_workers = workers or sc.workers or default_workers()
    n_workers = max(1, min(n_workers, math.ceil(prof.max_rps)))
    run_id = run_id or uuid.uuid4().hex
    run_name = name or sc.name
    config = redact_config(sc.to_dict())  # what sinks/results keep: never raw secrets
    config["workers"] = n_workers
    created_at = time.time()
    meta = RunMeta(id=run_id, name=run_name, created_at=created_at, config=config)

    sinks = MultiSink([sink] if isinstance(sink, RunSink) else (sink or []))
    emit = on_event or (lambda kind, data: None)
    sinks.start(meta)
    emit("start", meta)

    engine = _Engine(sc, plans, n_workers, run_id, stop_event, sinks, emit)
    status, message = engine.execute()
    steps = engine.finished_steps
    summary = analyze(steps)
    if engine.stopped_reason:
        summary["stopped_reason"] = engine.stopped_reason
    if message:
        summary["message"] = message
    sinks.finish(run_id, status, summary)
    return RunResult(
        run_id=run_id,
        name=run_name,
        status=status,
        config=config,
        created_at=created_at,
        finished_at=time.time(),
        steps=steps,
        seconds=engine.seconds,
        summary=summary,
        message=message,
    )


class _Engine:
    def __init__(
        self,
        sc: Scenario,
        plans: list[StepPlan],
        n_workers: int,
        run_id: str,
        stop_event: Any,
        sinks: MultiSink,
        emit: EventCallback,
    ) -> None:
        self.sc = sc
        self.plans = plans
        self.n = n_workers
        self.run_id = run_id
        self.external_stop = stop_event
        self.sinks = sinks
        self.emit = emit
        self.aggs = {p.index: _StepAgg(p) for p in plans}
        self.finished_steps: list[StepResult] = []
        self.seconds: list[dict[str, Any]] = []
        self.window = _Window()
        self.stopped_reason: str | None = None
        self.start_epoch = 0.0
        self.exited: set[int] = set()
        self.errors: dict[int, str] = {}

    def _worker_cfg(self, k: int) -> dict[str, Any]:
        sc = self.sc
        return {
            "worker": k,
            "workers": self.n,
            "run_id": self.run_id,
            "parent_pid": os.getpid(),
            "base_url": sc.base_url,
            "headers": dict(sc.headers),
            "timeout_s": sc.timeout_s,
            "connections": sc.connections,
            "max_inflight": sc.max_inflight,
            "poisson": sc.profile.poisson,
            "seed": sc.seed,
            "requests": [
                {
                    "method": r.method,
                    "path": r.path,
                    "weight": r.weight,
                    "headers": dict(r.headers),
                    "json": r.json,
                    "has_json": r.has_json,
                    "body": r.body,
                    "expect_status": list(r.expect_status) if r.expect_status else None,
                }
                for r in sc.requests
            ],
            "steps": [(p.index, p.target_rps, p.start_s, p.duration_s) for p in self.plans],
            "_crash_after_s": sc.extra.get("_crash_after_s"),
        }

    # ------------------------------------------------------------------ lifecycle
    def execute(self) -> tuple[str, str | None]:
        from .worker import worker_main

        ctx = mp.get_context("spawn")
        self.q = ctx.Queue()
        self.go = ctx.Event()
        self.stop = ctx.Event()
        self.start_at = ctx.Value("d", 0.0)
        self.procs = [
            ctx.Process(
                target=worker_main,
                args=(self._worker_cfg(k), self.q, self.go, self.start_at, self.stop),
                name=f"redline-load-{k}",
                daemon=True,
            )
            for k in range(self.n)
        ]
        status, message = "done", None
        interrupted = 0
        try:
            for p in self.procs:
                p.start()
            try:
                status, message = self._wait_ready()
                if status == "done":
                    self.start_epoch = time.time() + START_DELAY_S
                    self.start_at.value = self.start_epoch
                    self.go.set()
                    status, message = self._loop()
            except KeyboardInterrupt:
                interrupted += 1
                status = "stopped"
                self._request_stop("interrupted (Ctrl-C)")
                status, message = self._loop(status="stopped")
        except KeyboardInterrupt:  # second Ctrl-C: give up waiting
            status = "stopped"
            interrupted += 1
        finally:
            self._shutdown(force=interrupted > 1 or status == "failed")
        self._finalize_remaining()
        return status, message

    def _wait_ready(self) -> tuple[str, str | None]:
        ready: set[int] = set()
        deadline = time.monotonic() + READY_TIMEOUT_S
        while len(ready) < self.n:
            if self._external_stop_set():
                self._request_stop("stopped before start")
                return "stopped", None
            try:
                msg = self.q.get(timeout=0.1)
            except queue_mod.Empty:
                msg = None
            if msg is not None:
                if msg["type"] == "ready":
                    ready.add(msg["worker"])
                elif msg["type"] == "error":
                    return "failed", f"worker {msg['worker']} failed: {msg['message']}"
            dead = self._dead_workers()
            if dead:
                return "failed", dead
            if time.monotonic() > deadline:
                return "failed", f"workers did not start within {READY_TIMEOUT_S:.0f}s"
        return "done", None

    def _external_stop_set(self) -> bool:
        ev = self.external_stop
        try:
            return bool(ev is not None and ev.is_set())
        except Exception:  # pragma: no cover - a broken stop object never kills the run
            return False

    def _request_stop(self, reason: str) -> None:
        if not self.stop.is_set():
            self.stopped_reason = self.stopped_reason or reason
            self.stop.set()
            self.emit("stopping", reason)

    def _dead_workers(self) -> str | None:
        for k, p in enumerate(self.procs):
            if k in self.exited or p.is_alive():
                continue
            # it may have exited cleanly with its "exit" still in the queue
            if p.exitcode == 0:
                continue
            if k in self.errors:
                return f"worker {k} failed: {self.errors[k]}"
            return f"worker {k} exited unexpectedly (exit code {p.exitcode})"
        return None

    def _loop(self, status: str = "done") -> tuple[str, str | None]:
        next_second = time.monotonic() + 1.0
        message: str | None = None
        dead_since: float | None = None
        while len(self.exited) < self.n:
            if status == "done" and self._external_stop_set():
                status = "stopped"
                self._request_stop("stop requested")
            try:
                msg = self.q.get(timeout=0.1)
            except queue_mod.Empty:
                msg = None
            if msg is not None:
                failure = self._handle(msg)
                if failure:
                    self._request_stop("worker failure")
                    return "failed", failure
            now = time.monotonic()
            if now >= next_second:
                self._emit_second()
                next_second = now + 1.0
            dead = self._dead_workers()
            if dead:
                # give the queue a moment to deliver a crashed worker's last message
                dead_since = dead_since or now
                if now - dead_since > 0.5:
                    self._request_stop("worker failure")
                    return "failed", dead
            # a worker that ended cleanly but whose "exit" got lost
            for k, p in enumerate(self.procs):
                if k not in self.exited and not p.is_alive() and p.exitcode == 0:
                    if dead_since is None:
                        dead_since = now
                    elif now - dead_since > 1.0:
                        self.exited.add(k)
            self._finalize_ready()
        self._emit_second()
        return status, message

    def _handle(self, msg: Mapping[str, Any]) -> str | None:
        kind = msg["type"]
        k = msg.get("worker", -1)
        if kind == "tick":
            for step, m in msg["steps"].items():
                agg = self.aggs.get(step)
                if agg is not None:
                    agg.add(m)
                self.window.add(m)
        elif kind == "step_done":
            agg = self.aggs.get(msg["step"])
            if agg is not None:
                agg.done[k] = (msg["until"], msg["partial"])
        elif kind == "exit":
            self.exited.add(k)
        elif kind == "error":
            self.errors[k] = msg["message"]
            return f"worker {k} failed: {msg['message']}"
        self._finalize_ready()
        return None

    # ------------------------------------------------------------------ steps
    def _finalize_ready(self) -> None:
        for plan in self.plans:
            agg = self.aggs[plan.index]
            if agg.final:
                continue
            if not all(k in agg.done or k in self.exited for k in range(self.n)):
                return  # steps are finalized in order
            self._finalize(agg)

    def _finalize_remaining(self) -> None:
        for plan in self.plans:
            agg = self.aggs[plan.index]
            if not agg.final:
                self._finalize(agg)

    def _finalize(self, agg: _StepAgg) -> None:
        agg.final = True
        started = bool(agg.done) or agg.sent or agg.dropped or agg.completed_any()
        if not started:
            return
        plan = agg.plan
        partial = any(p for _, p in agg.done.values()) or len(agg.done) < self.n
        until = max((u for u, _ in agg.done.values()), default=0.0)
        if not agg.done:
            until = plan.duration_s
        duration = until if partial else plan.duration_s
        result = StepResult(
            step=plan.index,
            target_rps=plan.target_rps,
            started_at=self.start_epoch + plan.start_s,
            ended_at=self.start_epoch + plan.start_s + duration,
            duration_s=duration,
            sent=agg.sent,
            ok=agg.ok,
            errors=agg.errors,
            dropped=agg.dropped,
            error_breakdown=dict(agg.breakdown),
            hist=agg.hist,
            service_hist=agg.svc,
            partial=partial,
        )
        if result.sent == 0 and result.dropped == 0 and partial:
            return
        self.finished_steps.append(result)
        self.sinks.step(self.run_id, result)
        self.emit("step", result)
        if not partial and not self.stop.is_set():
            reason = self.sc.profile.stop_on.breached(result.error_rate, result.p(95))
            if reason:
                self._request_stop(f"stop_on at step {result.step}: {reason}")

    def _emit_second(self) -> None:
        w = self.window
        if not (w.sent or w.ok or w.errors or w.dropped):
            return
        elapsed = time.time() - self.start_epoch
        current = None
        for plan in self.plans:
            if plan.start_s <= elapsed:
                current = plan
        sec = {
            "t": round(elapsed, 3),
            "step": current.index if current else None,
            "target_rps": current.target_rps if current else None,
            "sent": w.sent,
            "ok": w.ok,
            "errors": w.errors,
            "dropped": w.dropped,
            "p50_ms": round(w.hist.percentile(50), 3) if w.hist.count else 0.0,
            "p95_ms": round(w.hist.percentile(95), 3) if w.hist.count else 0.0,
        }
        self.seconds.append(sec)
        self.emit("second", sec)
        w.reset()

    # ------------------------------------------------------------------ teardown
    def _shutdown(self, *, force: bool) -> None:
        self.stop.set()
        self.go.set()  # release workers still waiting to start
        deadline = time.monotonic() + (0.5 if force else self.sc.timeout_s + EXIT_GRACE_S)
        # keep draining so workers never block on a full pipe while exiting
        while any(p.is_alive() for p in self.procs) and time.monotonic() < deadline:
            try:
                msg = self.q.get(timeout=0.05)
            except (queue_mod.Empty, OSError, EOFError):
                continue
            except KeyboardInterrupt:
                force = True
                deadline = min(deadline, time.monotonic() + 0.5)
                continue
            try:
                self._handle(msg)
            except Exception:  # pragma: no cover
                pass
        for p in self.procs:
            if p.is_alive():
                p.terminate()
        for p in self.procs:
            p.join(timeout=2.0)
            if p.is_alive():  # pragma: no cover
                p.kill()
                p.join(timeout=1.0)
        # late messages (final ticks) still in the queue
        while True:
            try:
                msg = self.q.get(timeout=0.05)
            except (queue_mod.Empty, OSError, EOFError):
                break
            self._handle(msg)
        self.q.close()
        self.q.cancel_join_thread()
