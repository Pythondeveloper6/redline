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
from .flows import ExtractError, check_expect, render_vars, render_vars_value, run_extract
from .profile import arrival_offsets
from .scenario import render_path
from .variation import (
    DataFeed,
    DataSource,
    IdentityPicker,
    TokenManager,
    identity_tag,
    load_identities,
    parse_auth,
    render_data,
    render_value,
)

__all__ = ["worker_main"]

FLUSH_EVERY_S = 1.0
STOP_POLL_S = 0.05


class _Acc:
    __slots__ = ("breakdown", "dropped", "errors", "hist", "identities", "ok", "sent", "svc")

    def __init__(self) -> None:
        self.sent = 0
        self.ok = 0
        self.errors = 0
        self.dropped = 0
        self.breakdown: Counter[str] = Counter()
        self.identities: Counter[str] = Counter()
        self.hist = Histogram()
        self.svc = Histogram()

    def to_msg(self) -> dict[str, Any]:
        return {
            "identities": dict(self.identities),
            "sent": self.sent,
            "ok": self.ok,
            "errors": self.errors,
            "dropped": self.dropped,
            "breakdown": dict(self.breakdown),
            "hist": self.hist.to_bytes() if self.hist.count else b"",
            "svc": self.svc.to_bytes() if self.svc.count else b"",
        }


class _Ctx:
    """The per-arrival state shared by every request of a flow."""

    __slots__ = ("headers", "lookup", "run_value", "variables")

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.lookup: Any = None
        self.run_value = ""
        self.variables: dict[str, str] = {}


class _StepAcc:
    __slots__ = ("count", "errors", "hist")

    def __init__(self) -> None:
        self.count = 0
        self.errors = 0
        self.hist = Histogram()


class _FlowAcc:
    """Per-flow numbers since the last flush: flows run, failed, their latency and each step."""

    def __init__(self, steps: int) -> None:
        self.count = 0
        self.errors = 0
        self.breakdown: Counter[str] = Counter()
        self.hist = Histogram()
        self.steps = [_StepAcc() for _ in range(steps)]

    def to_msg(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "errors": self.errors,
            "breakdown": dict(self.breakdown),
            "hist": self.hist.to_bytes() if self.hist.count else b"",
            "steps": [
                {
                    "count": s.count,
                    "errors": s.errors,
                    "hist": s.hist.to_bytes() if s.hist.count else b"",
                }
                for s in self.steps
            ],
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
        self.items: list[tuple[str, dict[str, Any]]] = [
            ("req", r) for r in cfg["requests"] if r["weight"] > 0
        ] + [("flow", f) for f in cfg.get("flows") or () if f["weight"] > 0]
        self.weights = [spec["weight"] for _, spec in self.items]
        self.flow_window: dict[str, _FlowAcc] = {}
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
        # what varies between requests (all optional; see load/variation.py)
        identities = load_identities(
            cfg.get("identities") or (), cfg.get("identities_file"), cfg.get("base_dir")
        )
        self.picker = (
            IdentityPicker(identities, cfg.get("identity_mode", "round_robin"), self.k, self.n)
            if identities
            else None
        )
        sources = [DataSource(**d) for d in cfg.get("data") or ()]
        self.feed = DataFeed(sources, self.k, self.n) if sources else None
        spec = parse_auth(cfg.get("auth"))
        self.tokens = TokenManager(spec, cfg.get("base_url")) if spec else None
        self.auth_error: str | None = None

    # ------------------------------------------------------------------ plumbing
    def _acc(self, step: int) -> _Acc:
        acc = self.window.get(step)
        if acc is None:
            acc = self.window[step] = _Acc()
        return acc

    def flush(self) -> None:
        if not self.window and not self.flow_window:
            return
        steps = {n: acc.to_msg() for n, acc in self.window.items()}
        flows = {name: acc.to_msg() for name, acc in self.flow_window.items()}
        self.window = {}
        self.flow_window = {}
        msg: dict[str, Any] = {
            "type": "tick",
            "worker": self.k,
            "t": time.perf_counter() - self.t0,
            "steps": steps,
        }
        if flows:
            msg["flows"] = flows
        self.q.put(msg)

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

    def _parent_gone(self) -> bool:
        """The process that spawned this worker died (it was killed): stop sending."""
        parent = self.cfg.get("parent_pid")
        if not parent:
            return False
        if os.name == "nt":  # pragma: no cover - there the pid does not change
            from .control import _pid_alive

            return not _pid_alive(int(parent))
        return os.getppid() != parent

    async def _watch(self) -> None:
        while not self.stopping:
            if self.stop_event.is_set() or self._parent_gone():
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
            if self.tokens is not None:  # a first token before any load; failure fails the run
                try:
                    await self.tokens.refresh()
                except Exception as exc:
                    raise RuntimeError(f"could not get an auth token: {exc}") from None
            self.q.put({"type": "ready", "worker": self.k, "pid": os.getpid()})
            loop = asyncio.get_running_loop()
            while not go.is_set():
                if self.stop_event.is_set() or self._parent_gone():
                    return
                await loop.run_in_executor(None, go.wait, 0.1)
            # shared epoch start → this process's perf_counter timeline
            self.t0 = time.perf_counter() + (start_at.value - time.time())
            watcher = asyncio.create_task(self._watch())
            flusher = asyncio.create_task(self._flusher())
            refresher = asyncio.create_task(self._refresh_token()) if self.tokens else None
            try:
                await self._schedule()
                await self._drain(float(cfg["timeout_s"]) + 2.0)
            finally:
                watcher.cancel()
                flusher.cancel()
                if refresher is not None:
                    refresher.cancel()
                self.flush()
            if self.auth_error:
                raise RuntimeError(self.auth_error)

    async def _refresh_token(self) -> None:
        """Refresh before expiry, off the request path; two failures in a row stop the run."""
        tokens = self.tokens
        assert tokens is not None
        while not self.stopping:
            wait = tokens.due_in()
            if wait is None:
                return
            await asyncio.sleep(max(wait, 0.5))
            for attempt in (1, 2):
                try:
                    await tokens.refresh()
                    break
                except Exception as exc:
                    if attempt == 2:
                        self.auth_error = f"token refresh failed: {exc}"
                        self.stopping = True
                        return
                    await asyncio.sleep(2.0)

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
                item = (
                    self.items[0]
                    if len(self.items) == 1
                    else self.rng.choices(self.items, self.weights)[0]
                )
                task = asyncio.create_task(self._one(at, index, item))
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

    def _new_ctx(self, step: int) -> _Ctx:
        """What one arrival shares across all its requests: the identity, the data row
        and the variables a flow extracts."""
        ctx = _Ctx()
        ctx.lookup = self.feed.new_context(self.rng) if self.feed is not None else None
        ctx.headers = dict(self.headers)
        if self.tokens is not None:
            pair = self.tokens.header()
            if pair:
                ctx.headers[pair[0]] = pair[1]
        ctx.run_value = f"{self.run_header};step={step}"
        if self.picker is not None:
            identity = self.picker.pick(self.rng)
            ctx.headers.update(identity.request_headers())
            ctx.run_value += f";identity={identity_tag(identity.name)}"
            self._acc(step).identities[identity.name] += 1
        return ctx

    async def _send(
        self, req: dict[str, Any], ctx: _Ctx, flow_tag: str = ""
    ) -> tuple[str | None, float | None]:
        """Send one request. Returns ``(failure, sent_at)``: ``failure`` is ``None`` for a
        success, else the error class (a status code, an exception name, ``assertion`` or
        ``extract``)."""
        sent_at: float | None = None
        try:
            lookup, variables = ctx.lookup, ctx.variables
            headers = dict(ctx.headers)
            headers.update(
                {
                    k: render_vars(render_data(v, lookup), variables)
                    for k, v in req.get("headers", {}).items()
                }
            )
            headers["X-Redline-Run"] = ctx.run_value + flow_tag
            kwargs: dict[str, Any] = {"headers": headers}
            if req.get("has_json"):
                body = render_vars_value(render_value(req["json"], lookup), variables)
                kwargs["content"] = jsonlib.dumps(body).encode()
                headers.setdefault("Content-Type", "application/json")
            elif req.get("body") is not None:
                kwargs["content"] = render_vars(
                    render_data(req["body"], lookup), variables
                ).encode()
            path = render_path(req["path"], self.rng, lookup, variables)
            async with self.sem:
                sent_at = time.perf_counter()
                response = await self.client.request(req["method"], path, **kwargs)
            elapsed_ms = (time.perf_counter() - sent_at) * 1000.0
        except asyncio.CancelledError:
            raise
        except ExtractError:
            return "extract", sent_at
        except Exception as exc:
            return type(exc).__name__, sent_at
        status = response.status_code
        expect = req.get("expect_status")
        if not (status in expect if expect else status < 400):
            return str(status), sent_at
        rules = req.get("expect") or ()
        if rules and check_expect(rules, response.headers, response.content) is not None:
            return "assertion", sent_at
        if req.get("max_ms") and elapsed_ms > req["max_ms"]:
            return "assertion", sent_at
        specs = req.get("extract")
        if specs:
            try:
                ctx.variables.update(run_extract(specs, response.headers, response.content))
            except ExtractError:
                return "extract", sent_at
        return None, sent_at

    async def _one(self, scheduled: float, step: int, item: tuple[str, dict[str, Any]]) -> None:
        kind, spec = item
        sent_at: float | None = None
        failure: str | None = None
        try:
            ctx = self._new_ctx(step)
            if kind == "req":
                failure, sent_at = await self._send(spec, ctx)
            else:
                failure, sent_at = await self._flow(scheduled, spec, ctx)
        except asyncio.CancelledError:
            failure = "Cancelled"
            raise
        except Exception as exc:  # never leave the in-flight count behind
            failure = type(exc).__name__
        finally:
            end = time.perf_counter()
            acc = self._acc(step)
            acc.hist.record((end - scheduled) * 1000.0)
            if sent_at is not None:
                acc.svc.record((end - sent_at) * 1000.0)
            if failure is None:
                acc.ok += 1
            else:
                acc.errors += 1
                acc.breakdown[failure or "Error"] += 1
            self.inflight -= 1
            self.inflight_by_step[step] -= 1
            self._maybe_done(step)

    async def _flow(
        self, scheduled: float, flow: dict[str, Any], ctx: _Ctx
    ) -> tuple[str | None, float | None]:
        """Run a flow's steps in order; the first failure ends it and counts once."""
        name, n_steps = flow["name"], len(flow["steps"])
        first_sent: float | None = None
        failure: str | None = None
        for index, req in enumerate(flow["steps"]):
            tag = f";flow={identity_tag(flow['name'])};fstep={index + 1}"
            started = time.perf_counter()
            failure, sent_at = await self._send(req, ctx, tag)
            if first_sent is None:
                first_sent = sent_at
            # looked up each time: a flush between steps swaps the accumulator out
            stats = self._flow_acc(name, n_steps)
            step_stats = stats.steps[index]
            step_stats.count += 1
            step_stats.hist.record((time.perf_counter() - started) * 1000.0)
            if failure is not None:
                step_stats.errors += 1
                stats.breakdown[failure] += 1
                break
        stats = self._flow_acc(name, n_steps)
        stats.count += 1
        stats.hist.record((time.perf_counter() - scheduled) * 1000.0)
        if failure is not None:
            stats.errors += 1
        return failure, first_sent

    def _flow_acc(self, name: str, steps: int) -> _FlowAcc:
        acc = self.flow_window.get(name)
        if acc is None:
            acc = self.flow_window[name] = _FlowAcc(steps)
        return acc
