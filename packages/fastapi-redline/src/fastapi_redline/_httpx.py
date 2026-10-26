"""Outbound ``httpx`` calls as ``http_out`` spans.

Call :func:`instrument_httpx` once at startup (``install()`` does not do it for you, so
the patch is always an explicit choice). It wraps ``httpx.Client.send`` and
``httpx.AsyncClient.send``, which every request method funnels through, so all clients -
including ones created by libraries - are covered. Outside a traced request the wrapper
is one ``ContextVar.get()``.
"""

from __future__ import annotations

import time
from typing import Any

from redline_core import record_span
from redline_core.recorder import current

__all__ = ["instrument_httpx", "uninstrument_httpx"]

_perf = time.perf_counter
_originals: dict[str, Any] = {}


def _record(request: Any, start: float, status: int | None) -> None:
    builder = current()
    if builder is None:
        return
    url = request.url
    redacted = builder.recorder.redactor.url(str(url))
    path = url.raw_path.decode("ascii", "replace").split("?")[0]
    record_span(
        "http_out",
        f"{request.method} {url.host}{path}",
        start,
        _perf() - start,
        {"method": request.method, "url": redacted, "status": status},
    )


def instrument_httpx() -> bool:
    """Patch httpx; returns ``False`` if httpx is not installed. Idempotent."""
    try:
        import httpx
    except ImportError:
        return False
    if _originals:
        return True
    sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send
    _originals.update(sync=sync_send, async_=async_send)

    def send(self: Any, request: Any, **kwargs: Any) -> Any:
        if current() is None:
            return sync_send(self, request, **kwargs)
        start, status = _perf(), None
        try:
            response = sync_send(self, request, **kwargs)
            status = response.status_code
            return response
        finally:
            _record(request, start, status)

    async def asend(self: Any, request: Any, **kwargs: Any) -> Any:
        if current() is None:
            return await async_send(self, request, **kwargs)
        start, status = _perf(), None
        try:
            response = await async_send(self, request, **kwargs)
            status = response.status_code
            return response
        finally:
            _record(request, start, status)

    httpx.Client.send = send  # type: ignore[method-assign]
    httpx.AsyncClient.send = asend  # type: ignore[method-assign]
    return True


def uninstrument_httpx() -> None:
    """Undo :func:`instrument_httpx`."""
    if not _originals:
        return
    import httpx

    httpx.Client.send = _originals.pop("sync")  # type: ignore[method-assign]
    httpx.AsyncClient.send = _originals.pop("async_")  # type: ignore[method-assign]
