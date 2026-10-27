"""Event-loop blocking detection: ``blocking`` spans (``CONTRACT.md`` §8, §9, §10).

Async adapters wrap each request in :func:`track_request`::

    builder = recorder.start_trace(...)
    async with track_request(builder):
        await app(scope, receive, send)
    recorder.finish_trace(builder, ...)

While at least one tracked request is in flight on an event loop, two things run – and
nothing at all otherwise (the cost of an idle process is zero):

* **A lag monitor** on that loop: a timer that re-arms itself every 5 ms
  (``loop.call_later``, no task) and measures how late it fires. A tick that is late by
  ``blocking_ms`` or more means the loop was stalled; the stall becomes a ``blocking`` span
  (``reason="loop_lag"``) on the request that caused it, or – when the culprit cannot be
  told apart – on every request in flight on that loop.
* **A watchdog thread** (one per process, parked on an ``Event`` one second after the last
  tracked request ends)
  that notices a tick overdue by half of ``blocking_ms`` and samples the loop thread's
  stack with ``sys._current_frames()``. That sample gives the stall its callsite (the
  innermost project frame), the blocking call (``time.sleep``, ``requests.get``,
  ``sync ORM``…) and, through ``asyncio.current_task(loop)``, the request it belongs to.

Separately, :func:`note_sync_call` lets SQL hooks mark a *synchronous* driver call made on
the loop thread itself (a sync ORM call inside an ``async def`` view) as a ``blocking``
span with ``reason="sync_call"``; a loop stall that overlaps one is not recorded twice.

A stall that is still in progress when the request finishes (``time.sleep`` and then
``return`` – the monitor never got the loop back) is flushed from
:meth:`Recorder.finish_trace` through ``TraceBuilder.on_finish``.
"""

from __future__ import annotations

import asyncio
import contextvars
import linecache
import os
import re
import sys
import threading
import time
import weakref
from types import FrameType
from typing import TYPE_CHECKING, Any

from . import recorder as _rec
from .model import Callsite, Span, new_id

if TYPE_CHECKING:
    from .callsite import CallsiteCapturer
    from .recorder import TraceBuilder

__all__ = ["INTERVAL", "RequestTracker", "call_name", "note_sync_call", "track_request"]

#: How often the lag monitor ticks while requests are in flight (seconds).
INTERVAL = 0.005
#: Seconds the watchdog keeps polling (every 10 ms, so a new request is watched at once)
#: after the last request ends, before it parks on an event.
WATCHDOG_LINGER = 1.0
#: ``sync_call`` spans shorter than this are not recorded (milliseconds).
MIN_SYNC_CALL_MS = 1.0

_perf = time.perf_counter
_lock = threading.Lock()
_monitors: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopMonitor] = (
    weakref.WeakKeyDictionary()
)
#: Monitors with at least one tracked request (read by the watchdog).
_active: set[_LoopMonitor] = set()
#: Thread idents currently running a monitored loop (``note_sync_call``'s fast path).
_loop_threads: dict[int, int] = {}
_wake = threading.Event()
_watchdog: threading.Thread | None = None


class _Sample:
    """What the watchdog saw on the loop thread during one overdue tick."""

    __slots__ = ("call", "callsite", "culprit", "seq")

    def __init__(
        self, seq: int, callsite: Callsite | None, call: str | None, culprit: RequestTracker | None
    ) -> None:
        self.seq = seq
        self.callsite = callsite
        self.call = call
        self.culprit = culprit


class RequestTracker:
    """Returned by :func:`track_request`: a sync and async context manager.

    Entering it outside a running event loop, for an inert builder, or with
    ``detect_blocking`` off does nothing.
    """

    __slots__ = ("attributed", "builder", "monitor", "task")

    def __init__(self, builder: TraceBuilder | None) -> None:
        self.builder = builder
        self.monitor: _LoopMonitor | None = None
        self.task: asyncio.Task[Any] | None = None
        self.attributed = -1  # the monitor tick whose stall was last recorded here

    def __enter__(self) -> RequestTracker:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    async def __aenter__(self) -> RequestTracker:
        self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.stop()

    def start(self) -> None:
        builder = self.builder
        if (
            self.monitor is not None
            or builder is None
            or not builder.active
            or builder.finished
            or not builder.recorder.settings.detect_blocking
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop in this thread: nothing can block it
            return
        try:
            self.task = asyncio.current_task(loop)
            monitor = _monitor_for(loop)
            monitor.add(self)
        except Exception:  # pragma: no cover - never break the request
            _rec.logger.exception("Redline failed to start the event-loop monitor")
            return
        self.monitor = monitor
        hooks = builder.on_finish
        if hooks is None:
            hooks = builder.on_finish = []
        hooks.append(self._on_finish)

    def flush(self) -> None:
        """Record a stall still in progress on this request now (called before finishing)."""
        monitor = self.monitor
        if monitor is not None:
            monitor.flush(self)

    def stop(self) -> None:
        monitor = self.monitor
        if monitor is None:
            return
        self.monitor = None
        try:
            monitor.flush(self)
        finally:
            monitor.remove(self)

    def _on_finish(self, builder: TraceBuilder) -> None:
        self.stop()


def track_request(builder: TraceBuilder | None) -> RequestTracker:
    """Watch the running event loop for stalls while ``builder``'s request is in flight.

    Use as ``async with track_request(builder):`` around the application call (or
    ``with``, from code already running on the loop). Stalls are attributed to the trace
    before :meth:`Recorder.finish_trace` returns, wherever that is called from.
    """
    return RequestTracker(builder)


def _monitor_for(loop: asyncio.AbstractEventLoop) -> _LoopMonitor:
    monitor = _monitors.get(loop)
    if monitor is None:
        with _lock:
            monitor = _monitors.get(loop)
            if monitor is None:
                monitor = _monitors[loop] = _LoopMonitor(loop, threading.get_ident())
    return monitor


class _LoopMonitor:
    """The lag timer of one event loop; ticks only while ``trackers`` is non-empty."""

    def __init__(self, loop: asyncio.AbstractEventLoop, thread_id: int) -> None:
        self.loop_ref = weakref.ref(loop)
        self.thread_id = thread_id
        self.trackers: list[RequestTracker] = []
        self.by_task: dict[Any, RequestTracker] = {}
        self.handle: asyncio.TimerHandle | None = None
        self.expected = 0.0  # perf_counter() reading the next tick is due at
        self.seq = 0
        self.sample: _Sample | None = None
        self.threshold = 0.02  # seconds
        self.capturer: CallsiteCapturer | None = None

    # membership (loop thread) ----------------------------------------------
    def add(self, tracker: RequestTracker) -> None:
        builder = tracker.builder
        assert builder is not None
        threshold = max(builder.recorder.settings.blocking_ms, 1.0) / 1000.0
        with _lock:
            first = not self.trackers
            self.trackers.append(tracker)
            if tracker.task is not None:
                self.by_task[tracker.task] = tracker
            if first:
                self.threshold = threshold
                self.capturer = builder.recorder.capturer
                self.thread_id = threading.get_ident()
                _active.add(self)
                _loop_threads[self.thread_id] = _loop_threads.get(self.thread_id, 0) + 1
            elif threshold < self.threshold:
                self.threshold = threshold
        if first:
            if self.handle is None:
                self._arm(_perf())
            if not _wake.is_set():  # the watchdog is parked: start it (rare under load)
                _ensure_watchdog()
                _wake.set()

    def remove(self, tracker: RequestTracker) -> None:
        with _lock:
            try:
                self.trackers.remove(tracker)
            except ValueError:
                return
            if tracker.task is not None and self.by_task.get(tracker.task) is tracker:
                del self.by_task[tracker.task]
            if self.trackers:
                return
            _active.discard(self)
            n = _loop_threads.get(self.thread_id, 0) - 1
            if n > 0:
                _loop_threads[self.thread_id] = n
            else:
                _loop_threads.pop(self.thread_id, None)
            handle, self.handle = self.handle, None
        if handle is not None:
            if threading.get_ident() == self.thread_id:
                handle.cancel()
            else:  # finished from another thread: cancel on the loop
                loop = self.loop_ref()
                if loop is not None and not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(handle.cancel)
                    except RuntimeError:  # pragma: no cover - loop closing
                        pass

    # ticking (loop thread) --------------------------------------------------
    def _arm(self, now: float) -> None:
        loop = self.loop_ref()
        if loop is None or loop.is_closed():
            self.handle = None
            return
        self.expected = now + INTERVAL
        # An empty context: the timer must not keep any request's context alive.
        self.handle = loop.call_later(INTERVAL, self._tick, context=contextvars.Context())

    def _tick(self) -> None:
        now = _perf()
        if now - self.expected >= self.threshold:
            try:
                self._stall(self.expected - INTERVAL, now, None)
            except Exception:  # pragma: no cover - never break the loop
                _rec.logger.exception("Redline failed to record an event-loop stall")
        self.seq += 1
        self.sample = None
        if self.trackers:
            self._arm(now)
        else:
            self.handle = None

    def flush(self, tracker: RequestTracker) -> None:
        """Attribute a stall still in progress to ``tracker`` (its request is finishing)."""
        if threading.get_ident() != self.thread_id or self.handle is None:
            return
        now = _perf()
        if now - self.expected >= self.threshold:
            self._stall(self.expected - INTERVAL, now, tracker)

    def _stall(self, start: float, end: float, only: RequestTracker | None) -> None:
        # ``start`` is the previous tick: the loop was last seen alive then, so the stall
        # began at most one interval later (it is never under-reported).
        sample = self.sample
        if sample is not None and sample.seq != self.seq:
            sample = None
        culprit = sample.culprit if sample is not None else None
        if only is not None:
            if culprit is not None and culprit is not only:
                return  # another request blocked the loop; it gets the span
            targets = [only]
        else:
            with _lock:
                targets = list(self.trackers)
            if culprit is not None:  # its own request (if still in flight) gets the span
                targets = [t for t in targets if t is culprit]
        for tracker in targets:
            if tracker.attributed == self.seq or tracker.builder is None:
                continue
            tracker.attributed = self.seq
            _record_stall(tracker.builder, start, end, sample)


def _record_stall(builder: TraceBuilder, start: float, end: float, sample: _Sample | None) -> None:
    if builder.finished:
        return
    start = max(start, builder.t0)
    if end <= start:
        return
    start_ms = (start - builder.t0) * 1000.0
    end_ms = (end - builder.t0) * 1000.0
    with builder._lock:
        for s in reversed(builder.spans):
            if (
                s.kind == "blocking"
                and s.data.get("reason") == "sync_call"
                and s.start_ms < end_ms
                and s.start_ms + s.duration_ms > start_ms
            ):
                return  # already explained by a sync driver call on the loop
    call = sample.call if sample is not None else None
    lag_ms = round(end_ms - start_ms, 3)
    builder.add(
        Span(
            id=new_id(),
            parent_id=None,
            kind="blocking",
            name=call or "event loop blocked",
            start_ms=round(start_ms, 3),
            duration_ms=lag_ms,
            callsite=sample.callsite if sample is not None else None,
            data={"reason": "loop_lag", "call": call, "lag_ms": lag_ms},
        )
    )


# ── sync driver calls on the loop thread ─────────────────────────────────────────────


def note_sync_call(span: Span | None, call: str = "sync ORM") -> Span | None:
    """Mark ``span`` (a just-recorded SQL span) as blocking if it ran on a monitored loop.

    SQL hooks of *synchronous* drivers call this right after recording. It costs one
    dict truthiness check when no async request is in flight. Returns the ``blocking``
    span, or ``None``.
    """
    if span is None or not _loop_threads or span.duration_ms < MIN_SYNC_CALL_MS:
        return None
    if threading.get_ident() not in _loop_threads:
        return None
    builder = _rec.current()
    if builder is None or not builder.on_finish:
        return None
    blocking = Span(
        id=new_id(),
        parent_id=span.parent_id,
        kind="blocking",
        name=call,
        start_ms=span.start_ms,
        duration_ms=span.duration_ms,
        callsite=span.callsite,
        data={
            "reason": "sync_call",
            "call": call,
            "lag_ms": span.duration_ms,
            "span_id": span.id,
        },
    )
    builder.add(blocking)
    return blocking


# ── the watchdog thread ──────────────────────────────────────────────────────────────


def _ensure_watchdog() -> None:
    global _watchdog
    thread = _watchdog
    if thread is not None and thread.is_alive():
        return
    with _lock:
        if _watchdog is not None and _watchdog.is_alive():
            return
        _watchdog = threading.Thread(target=_watch, name="redline-loop-watchdog", daemon=True)
        _watchdog.start()


def _after_fork() -> None:  # pragma: no cover - exercised in forked workers
    """A forked child has no watchdog thread and none of the parent's loops."""
    global _watchdog, _lock
    _lock = threading.Lock()
    _watchdog = None
    _wake.clear()
    _active.clear()
    _loop_threads.clear()
    _monitors.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _watch() -> None:
    idle_since: float | None = None
    while True:
        with _lock:
            monitors = list(_active)
        now = _perf()
        if not monitors:
            # Linger briefly before parking, so back-to-back requests do not pay for
            # waking this thread each time; parked, it costs nothing.
            if idle_since is None:
                idle_since = now
            if now - idle_since < WATCHDOG_LINGER:
                time.sleep(0.01)
                continue
            with _lock:
                if not _active:
                    _wake.clear()
            _wake.wait()
            idle_since = None
            continue
        idle_since = None
        delay = 0.05
        for monitor in monitors:
            half = monitor.threshold / 2
            due = monitor.expected + half
            if now >= due:
                seq = monitor.seq
                sample = monitor.sample
                if sample is None or sample.seq != seq:
                    try:
                        monitor.sample = _take_sample(monitor, seq)
                    except Exception:  # pragma: no cover - sampling is best effort
                        pass
                delay = min(delay, half)
            else:
                delay = min(delay, max(due - now, 0.001))
        time.sleep(max(delay, 0.001))


def _take_sample(monitor: _LoopMonitor, seq: int) -> _Sample:
    frame = sys._current_frames().get(monitor.thread_id)
    capturer = monitor.capturer
    callsite = call = None
    if frame is not None and capturer is not None:
        callsite = capturer.capture(frame=frame)
        call = call_name(frame, capturer)
    del frame
    return _Sample(seq, callsite, call, _culprit(monitor))


def _culprit(monitor: _LoopMonitor) -> RequestTracker | None:
    """The tracked request whose task is running on the stalled loop, when knowable."""
    loop = monitor.loop_ref()
    if loop is None:
        return None
    try:
        task = asyncio.current_task(loop)
    except Exception:
        return None
    if task is None:
        return None
    with _lock:
        tracker = monitor.by_task.get(task)
        if tracker is not None:
            return tracker
        trackers = list(monitor.trackers)
    get_context = getattr(task, "get_context", None)  # Python 3.12+
    if get_context is None:
        return None
    try:
        builder = get_context().get(_rec._trace)
    except Exception:
        return None
    if builder is None:
        return None
    return next((t for t in trackers if t.builder is builder), None)


# ── naming the blocking call ─────────────────────────────────────────────────────────

_CALL = re.compile(r"((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)\s*\(")
_KEYWORDS = frozenset(
    ("if", "elif", "while", "for", "return", "await", "assert", "not", "and", "or", "in",
     "print", "with", "lambda", "yield", "del", "def", "class")
)  # fmt: skip
_MAX_DEPTH = 64


def call_name(frame: FrameType, capturer: CallsiteCapturer) -> str | None:
    """Best-effort name of the blocking call the stack ``frame`` is in.

    Library frames between the innermost project frame and the top of the stack name the
    API the project called (``requests.get``, ``urllib.request.urlopen``, ``sync ORM``
    for anything under ``django.db``…). With no library frame (a C function such as
    ``time.sleep``) the project frame's source line is read for the callee.
    """
    chain: list[FrameType] = []
    f: FrameType | None = frame
    while f is not None and len(chain) < _MAX_DEPTH:
        chain.append(f)
        f = f.f_back
    project = next(
        (i for i, fr in enumerate(chain) if capturer.classify(fr.f_code.co_filename)), None
    )
    inner = chain[:project] if project is not None else chain
    modules = [(fr.f_globals.get("__name__") or "", fr.f_code.co_name) for fr in inner]
    for module, _ in modules:
        if module.startswith("django.db"):
            return "sync ORM"
    for module, _ in modules:
        if module.startswith("sqlalchemy."):
            return "SQLAlchemy (sync)"
    if modules:
        module, function = modules[-1]  # the API the project frame called
        top = module.split(".", 1)[0]
        if top == "requests":
            return f"requests.{function}" if module == "requests.api" else "requests"
        if top in ("asyncio", "selectors", "concurrent"):
            return None  # the loop itself, not a blocking call
        return f"{module}.{function}" if module else function
    if project is None:
        return None
    fr = chain[project]
    line = linecache.getline(fr.f_code.co_filename, fr.f_lineno or 0)
    for match in _CALL.finditer(line):
        name = match.group(1)
        if name not in _KEYWORDS:
            return name
    return None
