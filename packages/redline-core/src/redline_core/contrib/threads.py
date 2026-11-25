"""Attribute work on threads and pools to the request that started it.

A plain ``threading.Thread`` does not inherit :mod:`contextvars`, and neither does a
``ThreadPoolExecutor`` worker, so SQL, cache and HTTP calls made there are not recorded
against any request. Call :func:`instrument_threads` once at startup. It patches
``concurrent.futures.ThreadPoolExecutor.submit`` (and therefore ``map`` and
``loop.run_in_executor``) and ``threading.Thread.start`` so that, when a request is being
recorded, the work runs in a copy of the current context, exactly as
``contextvars.copy_context().run`` would. Spans recorded there

* belong to the request and keep the span that was open at the call as their parent;
* carry the worker's name in ``span.data["thread"]``.

Limits. Work that is still running when the request finishes is not recorded: the trace is
already closed, so later spans are dropped rather than attached to a finished request or
turned into a separate job. Threads are only given the context when started *inside* a
recorded request; threads started at import time or by Redline itself (names starting with
``redline``) are left alone. A worker thread that a pool creates while ``submit`` runs does
not keep the submitting request's context for its lifetime; each submitted task gets its
own copy.

Opt-in and process-wide; :func:`uninstrument_threads` restores both methods.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import threading
from typing import Any

from redline_core.recorder import _thread_label, current

__all__ = ["instrument_threads", "uninstrument_threads"]

_originals: dict[str, Any] = {}
#: True while a pool's ``submit`` runs, so the worker threads it starts keep a clean context.
_spawning: contextvars.ContextVar[bool] = contextvars.ContextVar("redline_spawning", default=False)


def _recording() -> bool:
    try:
        builder = current()
    except Exception:
        return False
    return builder is not None and not builder.finished


def _labelled(fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    _thread_label.set(threading.current_thread().name)
    return fn(*args, **kwargs)


def _run_in_context(
    ctx: contextvars.Context, fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return ctx.run(_labelled, fn, args, kwargs)


def instrument_threads() -> bool:
    """Patch ``ThreadPoolExecutor.submit`` and ``Thread.start``. Idempotent; always ``True``."""
    if _originals:
        return True
    submit = concurrent.futures.ThreadPoolExecutor.submit
    start = threading.Thread.start
    _originals.update(submit=submit, start=start)

    def patched_submit(self: Any, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        if not _recording():
            return submit(self, fn, *args, **kwargs)
        ctx = contextvars.copy_context()
        token = _spawning.set(True)
        try:
            return submit(self, _run_in_context, ctx, fn, args, kwargs)
        finally:
            _spawning.reset(token)

    def patched_start(self: Any) -> None:
        if _recording() and not _spawning.get() and not self.name.startswith("redline"):
            ctx = contextvars.copy_context()
            run = self.run

            def run_in_context() -> None:
                ctx.run(_labelled, run, (), {})

            self.run = run_in_context  # type: ignore[method-assign]
        start(self)

    concurrent.futures.ThreadPoolExecutor.submit = patched_submit  # type: ignore[method-assign,assignment]
    threading.Thread.start = patched_start  # type: ignore[method-assign]
    return True


def uninstrument_threads() -> None:
    """Undo :func:`instrument_threads`."""
    if not _originals:
        return
    concurrent.futures.ThreadPoolExecutor.submit = _originals.pop("submit")  # type: ignore[method-assign]
    threading.Thread.start = _originals.pop("start")  # type: ignore[method-assign]
