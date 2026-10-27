"""Access control for the Redline UI and API.

* :func:`local_only` – the default when ``Redline(permission=None)``: only loopback
  clients, addressing the server by a loopback **name** (see :func:`is_local_host_header`),
  get in. :func:`loopback_guard` builds a variant that also accepts other ``Host`` names.
* :func:`check_csrf` – always on for the API's mutating methods (see the function).
* :class:`RedlineHTTPError` – what the checks raise; the Redline blueprint renders it (and
  any ``HTTPException`` from your own ``permission`` callable) in the API's error shape,
  ``{"error": {"code": ..., "message": ...}}``.

A ``permission`` is any ``callable(request) -> bool``; the guards here are such callables.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "LOCAL_HOST_NAMES",
    "RedlineHTTPError",
    "check_csrf",
    "is_local_host_header",
    "local_only",
    "loopback_guard",
]

_SAFE = {"GET", "HEAD", "OPTIONS"}
#: ``Host`` names the loopback default accepts (besides loopback IP literals and
#: ``*.localhost``, which RFC 6761 reserves for loopback).
LOCAL_HOST_NAMES = frozenset({"localhost"})


class RedlineHTTPError(Exception):
    """A refusal with a machine-readable ``code`` for the API's error shape."""

    def __init__(
        self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def _is_loopback(host: str | None) -> bool:
    """Whether a *client address* is loopback (``None``/empty is a unix socket)."""
    if not host:
        return True
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _host_name(header: str) -> str:
    """``Host`` header -> lower-cased host without port (``[::1]:8000`` -> ``::1``)."""
    host = header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host.rstrip(".")


def is_local_host_header(header: str | None, extra: Iterable[str] = ()) -> bool:
    """Whether a ``Host`` header names this machine's loopback interface.

    Accepted: ``localhost`` and ``*.localhost``, loopback IP literals (``127.0.0.1``,
    ``[::1]``), with any port – plus the names in ``extra``. A missing ``Host`` (HTTP/1.0)
    is accepted, since a browser always sends one.
    """
    if header is None:
        return True
    name = _host_name(header)
    if not name:
        return True
    if name in LOCAL_HOST_NAMES or name.endswith(".localhost"):
        return True
    if name in {e.strip().lower() for e in extra}:
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


class _LoopbackGuard:
    """``callable(request) -> bool`` allowing loopback clients addressing a loopback Host."""

    def __init__(self, allowed_hosts: Iterable[str] = ()) -> None:
        self.extra = frozenset(h.strip().lower() for h in allowed_hosts)

    def refusal(self, request: Any) -> str | None:
        """Why ``request`` is refused, or ``None`` when it may pass."""
        if not _is_loopback(request.remote_addr):
            return (
                "Redline is only reachable from localhost. Pass "
                "Redline(app, permission=your_check) to expose it."
            )
        if not is_local_host_header(request.environ.get("HTTP_HOST"), self.extra):
            return (
                "Redline only answers requests addressed to localhost, 127.0.0.1 or [::1] "
                "(this request's Host header names another site, as in a DNS-rebinding "
                "attack). Use loopback_guard(allowed_hosts=[...]) to allow another name."
            )
        return None

    def __call__(self, request: Any) -> bool:
        return self.refusal(request) is None


def loopback_guard(*, allowed_hosts: Iterable[str] = ()) -> _LoopbackGuard:
    """A permission allowing only loopback clients that address a loopback ``Host``.

    Checking the ``Host`` header defeats **DNS rebinding**: a web page on
    ``evil.example`` that re-points its own DNS name at ``127.0.0.1`` can make your
    browser send requests to the local server, but they carry ``Host: evil.example``.
    ``allowed_hosts`` adds names (e.g. a ``/etc/hosts`` alias such as ``"devbox"``);
    ports are ignored.
    """
    return _LoopbackGuard(allowed_hosts)


#: The default permission: loopback clients, loopback ``Host`` names.
local_only = loopback_guard()


def check_csrf(request: Any) -> None:
    """Reject cross-site requests to the mutating endpoints (DELETE traces, POST explain).

    Browsers attach an ``Origin`` header to every cross-origin and every same-origin
    non-GET request, so it must match the host the request was addressed to. Requests
    without ``Origin`` (curl, scripts, server-to-server) cannot be forged by a web page and
    are allowed – they are still subject to ``permission``. ``Sec-Fetch-Site: cross-site``
    is rejected too. No change to the SPA is needed.

    This check alone does not stop DNS rebinding (there ``Origin`` and ``Host`` agree);
    the ``Host`` check of :func:`local_only`, or real authentication, does.
    """
    if request.method in _SAFE:
        return
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        raise RedlineHTTPError(403, "csrf_failed", "Cross-site request blocked.")
    origin = request.headers.get("Origin")
    if origin is None:
        return
    allowed = {
        h.strip().lower()
        for h in (request.headers.get("Host"), request.headers.get("X-Forwarded-Host"))
        if h
    }
    netloc = urlsplit(origin).netloc.lower() if origin != "null" else ""
    if netloc not in allowed:
        raise RedlineHTTPError(403, "csrf_failed", "Cross-origin request blocked.")
