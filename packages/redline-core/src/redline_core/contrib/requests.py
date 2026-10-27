"""Outbound ``requests`` calls as ``http_out`` spans.

Call :func:`instrument_requests` once at startup. It wraps ``requests.Session.send``, which
every ``requests.get/post/...`` call funnels through, so sessions created by libraries are
covered too. Outside a traced request the wrapper is one ``ContextVar.get()``.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

import requests

from redline_core import record_span
from redline_core.recorder import current

__all__ = ["instrument_requests", "uninstrument_requests"]

_perf = time.perf_counter
_originals: dict[str, Any] = {}


def _record(request: Any, start: float, status: int | None) -> None:
    builder = current()
    if builder is None:
        return
    url = str(request.url)
    parts = urlsplit(url)
    record_span(
        "http_out",
        f"{request.method} {parts.hostname or ''}{parts.path}",
        start,
        _perf() - start,
        {"method": request.method, "url": builder.recorder.redactor.url(url), "status": status},
    )


def instrument_requests() -> bool:
    """Patch ``requests``; always ``True`` once patched. Idempotent."""
    if _originals:
        return True
    original = requests.Session.send
    _originals["send"] = original

    def send(self: Any, request: Any, **kwargs: Any) -> Any:
        if current() is None:
            return original(self, request, **kwargs)
        start, status = _perf(), None
        try:
            response = original(self, request, **kwargs)
            status = response.status_code
            return response
        finally:
            _record(request, start, status)

    requests.Session.send = send  # type: ignore[method-assign]
    return True


def uninstrument_requests() -> None:
    """Undo :func:`instrument_requests`."""
    if _originals:
        requests.Session.send = _originals.pop("send")  # type: ignore[method-assign]
