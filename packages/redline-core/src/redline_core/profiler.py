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

    def stop(self) -> dict[str, Any] | None:
        """Stop and export; ``None`` if it never started or recorded nothing."""
        if not self._running:
            return None
        self._running = False
        session = self._profiler.stop()
        if session is None or not getattr(session, "sample_count", 1):
            return None
        return to_speedscope(self._profiler)

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
