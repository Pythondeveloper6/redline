"""Server-sent events for ``GET api/stream`` (live tail, ``CONTRACT.md`` §12/§15).

Adapters serve the stream themselves (it is not JSON), but the event loop is shared::

    since = parse_since(params)                    # FilterError -> 400 invalid_filter
    return StreamingResponse(sync_events(api, since), headers=SSE_HEADERS)  # WSGI
    return StreamingResponse(async_events(api, since, is_disconnected), ...)  # ASGI

The stream opens with a ``retry:`` hint and a ``: connected`` comment (so proxies and the
browser see bytes at once), then polls :meth:`RedlineAPI.poll` every
:data:`POLL_INTERVAL` seconds and sends one ``event: trace`` per new TraceSummary, a
``: ping`` comment every :data:`PING_INTERVAL` seconds and closes after
:data:`MAX_DURATION` seconds (ASGI) or :data:`SYNC_MAX_DURATION` seconds (WSGI, where an
open stream pins a worker thread). ``EventSource`` reconnects by itself, after the
``retry:`` delay and with ``Last-Event-ID``, so closing early loses nothing. Each poll is
one short read on the store; no database connection of the application and no lock is
held between polls.

The constants are read at call time, so tests can monkeypatch them.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from typing import Any

from .storage.base import FilterError

__all__ = [
    "MAX_DURATION",
    "PING_INTERVAL",
    "POLL_INTERVAL",
    "SSE_HEADERS",
    "SYNC_MAX_DURATION",
    "async_events",
    "format_event",
    "parse_since",
    "sync_events",
]

#: Seconds between two polls of the store.
POLL_INTERVAL = 1.0
#: Seconds between two ``: ping`` comments (keeps proxies from timing the stream out).
PING_INTERVAL = 15.0
#: Seconds after which the server closes the stream (the browser reconnects).
MAX_DURATION = 300.0
#: The same for :func:`sync_events`: a WSGI stream holds a worker thread while open, so it
#: is recycled much sooner (capped by :data:`MAX_DURATION` too).
SYNC_MAX_DURATION = 25.0
#: Reconnect delay suggested to ``EventSource`` (milliseconds).
RETRY_MS = 3_000
#: The same for :func:`sync_events`, which closes every :data:`SYNC_MAX_DURATION` seconds.
SYNC_RETRY_MS = 500

#: Response headers besides ``Content-Type: text/event-stream``.
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
CONTENT_TYPE = "text/event-stream; charset=utf-8"

PING = b": ping\n\n"


def _open(retry_ms: int) -> bytes:
    return f"retry: {retry_ms}\n: connected\n\n".encode()


def parse_since(params: Mapping[str, Any]) -> float:
    """``?since=<epoch seconds>`` (default: now). Raises :class:`FilterError`."""
    raw = params.get("since")
    if isinstance(raw, list):
        raw = raw[-1] if raw else None
    if raw in (None, ""):
        return time.time()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise FilterError(f"since must be epoch seconds, got {raw!r}") from None
    if value != value or value < 0:  # NaN / negative
        raise FilterError(f"since must be epoch seconds, got {raw!r}")
    return value


def parse_after(params: Mapping[str, Any], headers: Mapping[str, Any] | None = None) -> int | None:
    """The resume position: ``?after=<seq>`` or the ``Last-Event-ID`` header (an SSE
    ``id:`` is the trace's ``seq``). ``None`` when absent or not a number (then ``since``
    decides where the stream starts)."""
    raw = params.get("after")
    if isinstance(raw, list):
        raw = raw[-1] if raw else None
    if raw in (None, "") and headers is not None:
        raw = headers.get("Last-Event-ID") or headers.get("last-event-id")
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def format_event(summary: Mapping[str, Any]) -> bytes:
    """One ``event: trace`` frame; the SSE id is the trace's ``seq`` (insertion order) when
    the store provides one, else its trace id."""
    data = json.dumps(dict(summary), separators=(",", ":"), default=str)
    event_id = summary.get("seq", summary.get("id", ""))
    return f"event: trace\nid: {event_id}\ndata: {data}\n\n".encode()


class _Cursor:
    """The stream position. It starts at ``since`` (an epoch) or ``after`` (a ``seq``) and,
    once a summary carrying a ``seq`` is sent, follows insertion order: traces are saved
    when they *finish*, so paging by ``started_at`` would skip a slow request that began
    before already-streamed fast ones."""

    __slots__ = ("after", "api", "last_id", "since")

    def __init__(self, api: Any, since: float, after: int | None = None) -> None:
        self.api = api
        self.since = since
        self.after = after
        self.last_id: str | None = None

    def poll(self) -> list[bytes]:
        try:
            if self.after is not None:
                items = self.api.poll(self.since, self.last_id, after_seq=self.after)
            else:
                items = self.api.poll(self.since, self.last_id)
        except Exception:  # a store hiccup must not kill the stream; try next tick
            return []
        out = []
        for item in items:
            seq = item.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                self.after = seq
            started = item.get("started_at")
            if isinstance(started, (int, float)):
                self.since = float(started)
                self.last_id = item.get("id")
            out.append(format_event(item))
        return out


def sync_events(
    api: Any,
    since: float,
    after: int | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[bytes]:
    """The SSE byte stream for a WSGI/thread-per-request server. It closes after
    :data:`SYNC_MAX_DURATION` seconds so that open tabs do not pin worker threads; the
    browser reconnects after :data:`SYNC_RETRY_MS` and resumes from ``Last-Event-ID``."""
    cursor = _Cursor(api, since, after)
    yield _open(SYNC_RETRY_MS)
    limit = min(MAX_DURATION, SYNC_MAX_DURATION)
    start = last_ping = clock()
    while True:
        yield from cursor.poll()
        now = clock()
        if now - start >= limit:
            return
        if now - last_ping >= PING_INTERVAL:
            last_ping = now
            yield PING
        sleep(POLL_INTERVAL)


async def async_events(
    api: Any,
    since: float,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    *,
    after: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[bytes]:
    """The SSE byte stream for an ASGI server. Store reads run in a worker thread, so the
    event loop is never blocked; ``is_disconnected`` (e.g. Starlette's
    ``request.is_disconnected``) ends the stream early."""
    cursor = _Cursor(api, since, after)
    yield _open(RETRY_MS)
    start = last_ping = clock()
    while True:
        for frame in await asyncio.to_thread(cursor.poll):
            yield frame
        now = clock()
        if now - start >= MAX_DURATION:
            return
        if now - last_ping >= PING_INTERVAL:
            last_ping = now
            yield PING
        await asyncio.sleep(POLL_INTERVAL)
        if is_disconnected is not None and await is_disconnected():
            return
