"""Resource metrics beside a load run's latency: which resource ran out?

The knee says when the system stopped keeping up and the bottleneck table says which query
slowed; neither says whether the application CPU, the connection pool, memory or garbage
collection was the limit. While a load run that is registered in the store is in progress, the
application samples itself once a second (:class:`RunMetricsSampler`): process CPU, resident
memory, garbage-collection pauses, threads and the SQLAlchemy pool (size, checked out, time
spent waiting for a connection). Samples go to the store tagged with the run and its current
step (``Store.save_run_metrics``); each worker process samples itself, so a multi-worker
deployment is covered.

Nothing is sampled outside a registered, running run, and nothing here reads user data.
:func:`aggregate` summarises samples per step and :func:`limiting_resource` names the likely
limit ("saturated at step 8: connection pool exhausted, wait time 40x").
"""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

__all__ = [
    "RunMetricsSampler",
    "aggregate",
    "limiting_resource",
    "note_pool_wait",
    "sampler_for",
]

INTERVAL_S = 1.0
#: Sampling stops this long after the last request tagged with the run.
IDLE_S = 8.0

# time spent waiting for a pooled connection since the last sample (milliseconds)
_pool_wait_ms = 0.0
_gc_pause_ms = 0.0
_gc_started = 0.0
_lock = threading.Lock()


def note_pool_wait(ms: float) -> None:
    """Called by the SQLAlchemy integration for every checkout it times."""
    global _pool_wait_ms
    _pool_wait_ms += ms


def _gc_callback(phase: str, info: dict[str, Any]) -> None:
    global _gc_started, _gc_pause_ms
    if phase == "start":
        _gc_started = time.perf_counter()
    elif _gc_started:
        _gc_pause_ms += (time.perf_counter() - _gc_started) * 1000.0
        _gc_started = 0.0


def _rss_mb() -> float | None:
    try:
        import psutil

        return float(psutil.Process().memory_info().rss) / 1_048_576
    except Exception:
        pass
    try:  # Linux
        with open("/proc/self/statm", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576
    except Exception:
        return None


def _pools() -> list[Any]:
    mod = sys.modules.get("redline_core.contrib.sqlalchemy")
    engines = getattr(mod, "_instrumented", None) if mod is not None else None
    if engines is None:
        return []
    return [e.pool for e in list(engines) if getattr(e, "pool", None) is not None]


def _pool_numbers() -> dict[str, float]:
    size = checked = capacity = 0.0
    found = False
    for pool in _pools():
        try:
            s = float(pool.size())
            c = float(pool.checkedout())
        except Exception:
            continue
        found = True
        size += s
        checked += c
        capacity += s + max(0.0, float(getattr(pool, "_max_overflow", 0) or 0))
    return (
        {"pool_size": size, "pool_checked_out": checked, "pool_capacity": capacity} if found else {}
    )


class RunMetricsSampler:
    """Samples this process while a registered load run is sending it traffic."""

    def __init__(self, store: Any, *, interval: float = INTERVAL_S) -> None:
        self.store = store
        self.interval = interval
        self._runs: dict[str, tuple[int, float]] = {}  # run_id -> (step, last tagged request)
        self._known: dict[str, tuple[bool, float]] = {}
        self._thread: threading.Thread | None = None
        self._pid = os.getpid()
        self._guard = threading.Lock()
        if _gc_callback not in gc.callbacks:
            gc.callbacks.append(_gc_callback)

    # called for every finished request that carried X-Redline-Run
    def note(self, run_id: str, step: Any) -> None:
        if not self._valid(run_id):
            return
        try:
            step_no = int(step)
        except (TypeError, ValueError):
            return  # the recording probe (step=probe) and other non-numeric steps
        with self._guard:
            self._runs[run_id] = (step_no, time.monotonic())
            self._ensure_thread()

    def _valid(self, run_id: str) -> bool:
        now = time.monotonic()
        hit = self._known.get(run_id)
        if hit is not None and now - hit[1] < (30.0 if hit[0] else 10.0):
            return hit[0]
        ok = False
        try:
            known = getattr(self.store, "is_known_run", None)  # a cached read: no reaping
            ok = bool(known(run_id)) if callable(known) else self.store.get_run(run_id) is not None
        except Exception:
            ok = False
        if len(self._known) > 256:  # bounded: random ids cannot grow it, or the query rate
            self._known.clear()
        self._known[run_id] = (ok, now)
        return ok

    def _ensure_thread(self) -> None:
        if os.getpid() != self._pid:  # forked: the thread did not come along
            self._pid = os.getpid()
            self._thread = None
            self._guard = threading.Lock()
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="redline-run-metrics", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        last_wall = time.perf_counter()
        last_cpu = time.process_time()
        while True:
            time.sleep(self.interval)
            now = time.monotonic()
            with self._guard:
                active = {r: v for r, v in self._runs.items() if now - v[1] < IDLE_S}
                self._runs = active
                if not active:
                    self._thread = None
                    return
            wall = time.perf_counter()
            cpu = time.process_time()
            global _pool_wait_ms, _gc_pause_ms
            with _lock:
                wait, pause = _pool_wait_ms, _gc_pause_ms
                _pool_wait_ms = _gc_pause_ms = 0.0
            elapsed = max(wall - last_wall, 1e-6)
            sample: dict[str, float] = {
                "cpu_pct": round((cpu - last_cpu) / elapsed * 100.0, 2),
                "gc_pause_ms": round(pause / elapsed, 3),  # per second
                "pool_wait_ms": round(wait / elapsed, 3),  # per second
                "threads": float(threading.active_count()),
            }
            rss = _rss_mb()
            if rss is not None:
                sample["rss_mb"] = round(rss, 2)
            sample.update(_pool_numbers())
            last_wall, last_cpu = wall, cpu
            for run_id, (step, _) in active.items():
                try:
                    self.store.save_run_metrics(run_id, step, sample, pid=os.getpid())
                except Exception:  # a failing store must never reach the application
                    pass


_SAMPLERS: dict[int, RunMetricsSampler] = {}


def sampler_for(store: Any) -> RunMetricsSampler | None:
    """The sampler for ``store`` (``None`` for stores that cannot hold run metrics)."""
    if not callable(getattr(store, "save_run_metrics", None)):
        return None
    key = id(store)
    sampler = _SAMPLERS.get(key)
    if sampler is None or sampler.store is not store:
        sampler = _SAMPLERS[key] = RunMetricsSampler(store)
    return sampler


# ── analysis ───────────────────────────────────────────────────────────────────────────

_NAMES = (
    "cpu_pct",
    "rss_mb",
    "gc_pause_ms",
    "threads",
    "pool_size",
    "pool_checked_out",
    "pool_capacity",
    "pool_wait_ms",
)


def aggregate(samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per step: the samples' count, the worker processes seen, and each metric's avg and max."""
    by_step: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for s in samples:
        by_step[int(s["step"])].append(s)
    out = []
    for step in sorted(by_step):
        rows = by_step[step]
        metrics: dict[str, dict[str, float]] = {}
        names = {k for r in rows for k in r if k not in ("step", "ts", "pid")}
        for name in sorted(names, key=lambda n: (n not in _NAMES, n)):
            values = [float(r[name]) for r in rows if isinstance(r.get(name), (int, float))]
            if values:
                metrics[name] = {
                    "avg": round(sum(values) / len(values), 3),
                    "max": round(max(values), 3),
                }
        out.append(
            {
                "step": step,
                "samples": len(rows),
                "workers": len({r.get("pid") for r in rows}),
                "metrics": metrics,
            }
        )
    return out


def _avg(step: Mapping[str, Any], name: str) -> float | None:
    m = step["metrics"].get(name)
    return m["avg"] if m else None


def _max(step: Mapping[str, Any], name: str) -> float | None:
    m = step["metrics"].get(name)
    return m["max"] if m else None


def limiting_resource(
    per_step: Sequence[Mapping[str, Any]], step_no: int | None
) -> dict[str, Any] | None:
    """The likely limiting resource at ``step_no`` (``None`` without any samples).

    In priority order: the connection pool (every connection checked out and requests waiting
    for one), application CPU (a worker near one full core), garbage collection (pauses of
    10% of the time or more), else ``unknown``. Memory growth over the run is reported as a
    note, never as the limit.
    """
    if not per_step:
        return None
    steps = {s["step"]: s for s in per_step}
    target = steps.get(step_no) if step_no is not None else None
    if target is None:
        target = per_step[-1]
    first = per_step[0]
    note = None
    rss_first, rss_last = _avg(first, "rss_mb"), _avg(per_step[-1], "rss_mb")
    if rss_first and rss_last and rss_last >= 1.3 * rss_first:
        note = f"memory grew from {rss_first:.0f} to {rss_last:.0f} MB over the run"
    wait = _avg(target, "pool_wait_ms")
    capacity = _max(target, "pool_capacity")
    checked = _max(target, "pool_checked_out")
    if (
        wait is not None
        and capacity
        and checked is not None
        and checked >= capacity
        and wait >= 5.0
    ):
        base = max(_avg(first, "pool_wait_ms") or 0.0, 1.0)
        return _verdict(
            "pool", target, note,
            f"connection pool exhausted ({checked:.0f}/{capacity:.0f} checked out), "
            f"wait time {wait / base:.0f}x the first step",
        )  # fmt: skip
    cpu = _avg(target, "cpu_pct")
    if cpu is not None and cpu >= 85.0:
        return _verdict("cpu", target, note, f"application CPU saturated ({cpu:.0f}% of a core)")
    gc_ms = _avg(target, "gc_pause_ms")
    if gc_ms is not None and gc_ms >= 100.0:
        return _verdict(
            "gc", target, note, f"garbage collection pauses ({gc_ms:.0f} ms per second)"
        )
    return _verdict("unknown", target, note, "no sampled resource was exhausted")


def _verdict(
    resource: str, step: Mapping[str, Any], note: str | None, detail: str
) -> dict[str, Any]:
    out = {"resource": resource, "step": step["step"], "detail": detail}
    if note:
        out["note"] = note
    return out


def run_sources(
    sources: Sequence[str],
    steps: Sequence[Any],
    save: Callable[[int, dict[str, float], float], None],
    *,
    timeout_s: float = 30.0,
) -> list[str]:
    """Call each ``module:function`` metric source once per step with ``(start, end)`` epochs.

    A source returns ``{name: number}`` (a cloud monitor, a metrics server, a host agent).
    ``save(step, values, ts)`` stores them. Returns the names of sources that failed; a failing
    source never fails the run.
    """
    import importlib
    from concurrent.futures import ThreadPoolExecutor

    failed: list[str] = []
    for spec in sources:
        try:
            module, _, attr = spec.partition(":")
            fn = getattr(importlib.import_module(module), attr)
        except Exception:
            failed.append(spec)
            continue
        for step in steps:
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    values = pool.submit(fn, step.started_at, step.ended_at).result(timeout_s)
                if isinstance(values, Mapping):
                    save(step.step, {str(k): v for k, v in values.items()}, step.ended_at)
            except Exception:
                failed.append(spec)
                break
    return failed
