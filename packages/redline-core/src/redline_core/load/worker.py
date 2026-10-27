"""The worker process: an asyncio loop with one ``httpx.AsyncClient``.

Started by :mod:`redline_core.load.engine` through the ``spawn`` multiprocessing context.
Protocol with the parent (every message is a dict on the shared result queue):

``ready``      the client is built; the worker now waits for the parent's ``go`` event and
               reads the shared start timestamp (epoch seconds).
``tick``       about once a second: per-step deltas since the previous tick
               (``sent``/``ok``/``errors``/``dropped``, error breakdown, latency and
               service-time histograms as ``Histogram.to_bytes()``).
``step_done``  every arrival of the step has been scheduled and all of its requests have
               finished; carries ``until`` (seconds of the step actually run) and
               ``partial`` (the step was cut short by a stop).
``exit``       clean end. ``error`` carries a message when the worker failed.

Open model: arrival times come from the step plan only (see
:func:`~redline_core.load.profile.arrival_offsets`), never from responses. Each request is
timed from its *scheduled* time, so waiting for a connection, a busy event loop or a slow
server all show up in the latency (coordinated-omission correction); the service time
(from the moment a connection slot was acquired) is kept in a second histogram. More
than ``max_inflight`` requests in flight → the arrival is counted ``dropped``.

SIGINT is ignored here: Ctrl-C is handled by the parent, which stops the workers through
the shared stop event so partial results are kept.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import os
import random
import signal
import time
import traceback
from collections import Counter
from typing import Any

from ..histogram import Histogram
from .profile import arrival_offsets
from .scenario import render_path

__all__ = ["worker_main"]

FLUSH_EVERY_S = 1.0
STOP_POLL_S = 0.05


class _Acc:
    __slots__ = ("breakdown", "dropped", "errors", "hist", "ok", "sent", "svc")

    def __init__(self) -> None:
        self.sent = 0
        self.ok = 0
        self.errors = 0
        self.dropped = 0
        self.breakdown: Counter[str] = Counter()
        self.hist = Histogram()
        self.svc = Histogram()

    def to_msg(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "ok": self.ok,
            "errors": self.errors,
            "dropped": self.dropped,
            "breakdown": dict(self.breakdown),
            "hist": self.hist.to_bytes() if self.hist.count else b"",
            "svc": self.svc.to_bytes() if self.svc.count else b"",
        }


def worker_main(cfg: dict[str, Any], out_q: Any, go: Any, start_at: Any, stop: Any) -> None:
    """Process entry point (must stay importable at module level for ``spawn``)."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    k = cfg["worker"]
    try:
        try:
            import httpx  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "the load tester needs httpx: pip install 'redline-core[load]'"
            ) from None
        asyncio.run(_Worker(cfg, out_q, stop).run(go, start_at))
    except BaseException as exc:
        out_q.put(
            {
                "type": "error",
                "worker": k,
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
        out_q.close()
        out_q.join_thread()
        raise SystemExit(1) from None
    out_q.put({"type": "exit", "worker": k})
    out_q.close()
    out_q.join_thread()


class _Worker:
    def __init__(self, cfg: dict[str, Any], out_q: Any, stop: Any) -> None:
        self.cfg = cfg
        self.k: int = cfg["worker"]
        self.n: int = cfg["workers"]
        self.q = out_q
        self.stop_event = stop
        self.stopping = False
        seed = cfg.get("seed")
        self.rng = random.Random(None if seed is None else seed * 1_000 + self.k)
        self.requests = [r for r in cfg["requests"] if r["weight"] > 0]
        self.weights = [r["weight"] for r in self.requests]
        self.headers = dict(cfg.get("headers") or {})
        self.run_header = cfg["run_id"]
        self.window: dict[int, _Acc] = {}
        self.inflight_by_step: Counter[int] = Counter()
        self.inflight = 0
        self.closed: dict[int, tuple[float, bool]] = {}
        self.done_sent: set[int] = set()
        self.tasks: set[asyncio.Task[None]] = set()
        self.t0 = 0.0
        self.crash_after = cfg.get("_crash_after_s")

    # ------------------------------------------------------------------ plumbing
    def _acc(self, step: int) -> _Acc:
        acc = self.window.get(step)
        if acc is None:
            acc = self.window[step] = _Acc()
        return acc

    def flush(self) -> None:
        if not self.window:
            return
        steps = {n: acc.to_msg() for n, acc in self.window.items()}
        self.window = {}
        self.q.put(
            {"type": "tick", "worker": self.k, "t": time.perf_counter() - self.t0, "steps": steps}
        )

    def _maybe_done(self, step: int) -> None:
        if step in self.done_sent or step not in self.closed:
            return
        if self.inflight_by_step[step] > 0:
            return
        self.flush()
        until, partial = self.closed[step]
        self.done_sent.add(step)
        self.q.put(
            {
                "type": "step_done",
                "worker": self.k,
                "step": step,
                "until": until,
                "partial": partial,
            }
        )

    async def _watch(self) -> None:
        while not self.stopping:
            if self.stop_event.is_set():
                self.stopping = True
                return
            await asyncio.sleep(STOP_POLL_S)

    async def _flusher(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_EVERY_S)
            self.flush()

    async def _sleep_until(self, at: float) -> None:
        while not self.stopping:
            delay = at - time.perf_counter()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, STOP_POLL_S))

    # ------------------------------------------------------------------ main
    async def run(self, go: Any, start_at: Any) -> None:
        import httpx

        cfg = self.cfg
        conns = int(cfg["connections"])
        limits = httpx.Limits(max_connections=conns, max_keepalive_connections=conns)
        timeout = httpx.Timeout(float(cfg["timeout_s"]))
        self.sem = asyncio.Semaphore(conns)
        async with httpx.AsyncClient(
            base_url=cfg["base_url"],
            limits=limits,
            timeout=timeout,
            http2=False,
            follow_redirects=False,
            headers={"User-Agent": "redline-load"},
        ) as client:
            self.client = client
            self.q.put({"type": "ready", "worker": self.k, "pid": os.getpid()})
            loop = asyncio.get_running_loop()
            while not go.is_set():
                if self.stop_event.is_set():
                    return
                await loop.run_in_executor(None, go.wait, 0.1)
            # shared epoch start → this process's perf_counter timeline
            self.t0 = time.perf_counter() + (start_at.value - time.time())
            watcher = asyncio.create_task(self._watch())
            flusher = asyncio.create_task(self._flusher())
            try:
                await self._schedule()
                await self._drain(float(cfg["timeout_s"]) + 2.0)
            finally:
                watcher.cancel()
                flusher.cancel()
                self.flush()

    async def _schedule(self) -> None:
        cfg = self.cfg
        poisson = bool(cfg.get("poisson"))
        max_inflight = int(cfg["max_inflight"])
        for index, rate, start_s, duration_s in cfg["steps"]:
            if self.stopping:
                break
            step_t0 = self.t0 + start_s
            await self._sleep_until(step_t0)
            if self.stopping:
                break
            for off in arrival_offsets(
                rate, duration_s, worker=self.k, workers=self.n, poisson=poisson, rng=self.rng
            ):
                at = step_t0 + off
                await self._sleep_until(at)
                if self.stopping:
                    break
                if self.crash_after is not None and at - self.t0 >= self.crash_after:
                    os._exit(3)  # test hook: simulate a hard crash
                acc = self._acc(index)
                if self.inflight >= max_inflight:
                    acc.dropped += 1
                    continue
                acc.sent += 1
                self.inflight += 1
                self.inflight_by_step[index] += 1
                req = (
                    self.requests[0]
                    if len(self.requests) == 1
                    else self.rng.choices(self.requests, self.weights)[0]
                )
                task = asyncio.create_task(self._one(at, index, req))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            await self._sleep_until(step_t0 + duration_s)
            now = time.perf_counter()
            partial = self.stopping and now < step_t0 + duration_s - 1e-3
            until = max(0.0, min(duration_s, now - step_t0)) if partial else duration_s
            self.closed[index] = (until, partial)
            self._maybe_done(index)

    async def _drain(self, grace_s: float) -> None:
        if self.tasks:
            _, pending = await asyncio.wait(set(self.tasks), timeout=grace_s)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for step in list(self.closed):
            self._maybe_done(step)

    async def _one(self, scheduled: float, step: int, req: dict[str, Any]) -> None:
        sent_at: float | None = None
        status: int | None = None
        error: str | None = None
        try:
            headers = {**self.headers, **req.get("headers", {})}
            headers["X-Redline-Run"] = f"{self.run_header};step={step}"
            kwargs: dict[str, Any] = {"headers": headers}
            if req.get("has_json"):
                kwargs["content"] = jsonlib.dumps(req["json"]).encode()
                headers.setdefault("Content-Type", "application/json")
            elif req.get("body") is not None:
                kwargs["content"] = req["body"].encode()
            path = render_path(req["path"], self.rng)
            async with self.sem:
                sent_at = time.perf_counter()
                response = await self.client.request(req["method"], path, **kwargs)
            status = response.status_code
        except asyncio.CancelledError:
            error = "Cancelled"
            raise
        except Exception as exc:
            error = type(exc).__name__
        finally:
            end = time.perf_counter()
            acc = self._acc(step)
            acc.hist.record((end - scheduled) * 1000.0)
            if sent_at is not None:
                acc.svc.record((end - sent_at) * 1000.0)
            if error is None and status is not None:
                expect = req.get("expect_status")
                good = status in expect if expect else status < 400
                if good:
                    acc.ok += 1
                else:
                    acc.errors += 1
                    acc.breakdown[str(status)] += 1
            else:
                acc.errors += 1
                acc.breakdown[error or "Error"] += 1
            self.inflight -= 1
            self.inflight_by_step[step] -= 1
            self._maybe_done(step)
