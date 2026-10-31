"""Record Starlette / FastAPI ``BackgroundTask`` runs as jobs linked to the request trace.

A background task runs after the response was sent, when the request trace is already
finished. :func:`instrument_background_tasks` captures the trace context when the task is
created (still inside the request) and records the later run with
:func:`redline_core.jobs.job`, so it shows up as its own ``TASK`` trace with the SQL and
other spans it made, tagged with the originating ``parent_trace_id``.
"""

from __future__ import annotations

from typing import Any

from starlette.background import BackgroundTask

from ..jobs import job, propagation_headers

__all__ = ["instrument_background_tasks", "uninstrument_background_tasks"]

_originals: dict[str, Any] = {}
_recorder: Any = None


def _task_name(func: Any) -> str:
    inner = getattr(func, "func", func)  # functools.partial
    module = getattr(inner, "__module__", None) or ""
    name = getattr(inner, "__qualname__", None) or type(inner).__name__
    return f"background:{module}.{name}" if module else f"background:{name}"


def instrument_background_tasks(recorder: Any = None) -> bool:
    """Patch ``BackgroundTask`` once per process; returns ``True`` when active."""
    global _recorder
    if _originals:
        if recorder is not None:
            _recorder = recorder
        return True
    _recorder = recorder
    init, call = BackgroundTask.__init__, BackgroundTask.__call__
    _originals.update(init=init, call=call)

    def __init__(self: Any, func: Any, *args: Any, **kwargs: Any) -> None:
        init(self, func, *args, **kwargs)
        try:
            self._redline_parent = propagation_headers() or None
        except Exception:
            self._redline_parent = None

    async def __call__(self: Any) -> None:
        with job(
            _task_name(self.func),
            recorder=_recorder,
            queue="starlette.background",
            parent=getattr(self, "_redline_parent", None),
        ):
            await call(self)

    BackgroundTask.__init__ = __init__  # type: ignore[method-assign]
    BackgroundTask.__call__ = __call__  # type: ignore[method-assign]
    return True


def uninstrument_background_tasks() -> None:
    """Undo :func:`instrument_background_tasks`."""
    global _recorder
    if _originals:
        BackgroundTask.__init__ = _originals["init"]  # type: ignore[method-assign]
        BackgroundTask.__call__ = _originals["call"]  # type: ignore[method-assign]
        _originals.clear()
    _recorder = None
