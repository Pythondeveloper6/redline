"""Optional per-request flamegraph profiling with `pyinstrument`_.

Install the extra to enable it::

    pip install "redline-core[profile]"

:class:`RequestProfiler` wraps one ``pyinstrument.Profiler`` per request. In async code
it runs with ``async_mode="enabled"``, so a profiler started inside one request's task only
attributes that task's work – concurrent requests on the same event loop each get their
own clean profile, and time spent awaiting shows up as an ``[await]`` frame instead of
someone else's code. The result is exported as `speedscope`_ JSON, which the UI renders as
a flamegraph and which can also be opened at https://www.speedscope.app.

.. _pyinstrument: https://pyinstrument.readthedocs.io
.. _speedscope: https://github.com/jlfwong/speedscope
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["ProfilerUnavailable", "RequestProfiler", "is_available", "require"]

_INSTALL_HINT = (
    "Redline's flamegraph profiler needs pyinstrument. Install it with "
    "`pip install \"redline-core[profile]\"`, or set profile='off'."
)


class ProfilerUnavailable(ImportError):
    """pyinstrument is not installed."""


def is_available() -> bool:
    """Whether pyinstrument can be imported."""
    try:
        import pyinstrument  # noqa: F401
    except ImportError:
        return False
    return True


def require() -> None:
    """Raise :class:`ProfilerUnavailable` with an install hint if pyinstrument is missing."""
    if not is_available():
        raise ProfilerUnavailable(_INSTALL_HINT)


class RequestProfiler:
    """Profiles one request; ``start()`` then ``stop()`` → speedscope dict."""

    def __init__(self, interval: float = 0.001, async_mode: str = "enabled") -> None:
        try:
            from pyinstrument import Profiler
        except ImportError as exc:
            raise ProfilerUnavailable(_INSTALL_HINT) from exc
        self._profiler = Profiler(interval=interval, async_mode=async_mode)
        self._running = False

    def start(self) -> None:
        self._profiler.start()
        self._running = True

    def stop(self, name: str | None = None, *, trim: bool = True) -> dict[str, Any] | None:
        """Stop and export; ``None`` if it never started or recorded nothing.

        ``name`` titles the profile (e.g. ``"GET /orders/ — shop.views.order_list"``). With
        ``trim`` the leading run of framework-internal frames is collapsed into one
        synthetic ``[framework] N frames`` root (see :func:`collapse_framework_frames`).
        """
        if not self._running:
            return None
        self._running = False
        session = self._profiler.stop()
        if session is None or not getattr(session, "sample_count", 1):
            return None
        doc = to_speedscope(self._profiler)
        if trim:
            collapse_framework_frames(doc)
        if name:
            doc["name"] = name
            for prof in doc.get("profiles", ()):
                prof["name"] = name
        return doc

    def abort(self) -> None:
        """Stop without exporting (used on error paths)."""
        if self._running:
            self._running = False
            try:
                self._profiler.stop()
            except Exception:  # pragma: no cover - defensive
                pass


def to_speedscope(profiler: Any) -> dict[str, Any]:
    """Render a stopped ``pyinstrument.Profiler`` as a speedscope document."""
    from pyinstrument.renderers import SpeedscopeRenderer

    return json.loads(profiler.output(SpeedscopeRenderer()))


#: Path fragments of frames that are plumbing rather than application code.
_FRAMEWORK_MARKERS: tuple[str, ...] = (
    "/django/core/handlers/",
    "/django/utils/deprecation.py",
    "/django/contrib/",
    "/django/middleware/",
    "/django_redline/",
    "/fastapi_redline/",
    "/redline_core/",
    "/starlette/",
    "/fastapi/",
    "/anyio/",
    "/asyncio/",
    "/uvicorn/",
    "/asgiref/",
)


def _is_framework_frame(frame: dict[str, Any]) -> bool:
    file = (frame.get("file") or "").replace("\\", "/")
    return any(marker in file for marker in _FRAMEWORK_MARKERS)


def collapse_framework_frames(doc: dict[str, Any]) -> int:
    """Collapse the leading framework-internal run of the root stack into one frame.

    Walks down the chain of frames that span the whole profile (Django middleware
    ``__call__``/``inner``/``_get_response``, the Starlette/FastAPI middleware stack, Redline's
    own frames) until the first application frame, and replaces them with a single
    ``[framework] N frames`` root covering the same time, so totals are unchanged.
    Returns the number of frames collapsed (0 leaves ``doc`` untouched).
    """
    frames = doc.get("shared", {}).get("frames", [])
    for prof in doc.get("profiles", ()):
        events = prof.get("events")
        if prof.get("type") != "evented" or not events:
            continue
        k = 0
        while 2 * (k + 1) <= len(events):
            opened, closed = events[k], events[-1 - k]
            if (
                opened["type"] != "O"
                or closed["type"] != "C"
                or opened["frame"] != closed["frame"]
                or not _is_framework_frame(frames[opened["frame"]])
            ):
                break
            k += 1
        if k == 0:
            continue
        frames.append({"name": f"[framework] {k} frame{'s' if k != 1 else ''}"})
        idx = len(frames) - 1
        first, last = events[0], events[-1]
        prof["events"] = [
            {"type": "O", "at": first["at"], "frame": idx},
            *events[k : len(events) - k],
            {"type": "C", "at": last["at"], "frame": idx},
        ]
        return k
    return 0
