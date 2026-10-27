"""Async helpers shared by ASGI adapters (``CONTRACT.md`` §14).

The event-loop lag monitor lives in :mod:`redline_core.blocking` (zero dependencies, so
WSGI adapters running async views through ``asgiref`` can use it too); this module is
the ASGI-facing entry point::

    from redline_core.contrib.asgi import track_request

    builder = recorder.start_trace(...)
    async with track_request(builder):
        await app(scope, receive, send)

The monitor runs only while at least one tracked request is in flight on the loop, and
stalls are attributed before :meth:`Recorder.finish_trace` returns – also when the trace
is finished from inside ``send`` (after the last body chunk).
"""

from __future__ import annotations

from typing import Any

from redline_core.blocking import RequestTracker, note_sync_call, track_request
from redline_core.recorder import TraceBuilder

__all__ = ["RequestTracker", "note_sync_call", "run_tracked", "track_request"]


async def run_tracked(builder: TraceBuilder | None, app: Any, scope: Any, receive: Any, send: Any):
    """``await app(scope, receive, send)`` with the event-loop lag monitor watching it."""
    async with track_request(builder):
        await app(scope, receive, send)
