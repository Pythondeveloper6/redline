"""Access control for the Redline UI and API.

* :func:`local_only` – the default when ``install(dependencies=None)``: only loopback
  clients, addressing the server by a loopback **name** (see the function), get in.
  :func:`loopback_guard` builds a variant that also accepts other ``Host`` names.
* :func:`csrf_guard` – always on for the API's mutating methods (see the function).
* :class:`RedlineHTTPError` – what the guards raise; the Redline router renders it (and
  any ``HTTPException`` from your own auth dependencies) in the API's error shape,
  ``{"error": {"code": ..., "message": ...}}``.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

__all__ = [
    "LOCAL_HOST_NAMES",
    "RedlineHTTPError",
    "csrf_guard",
    "is_local_host_header",
    "local_only",
    "loopback_guard",
]

_SAFE = {"GET", "HEAD", "OPTIONS"}
#: ``Host`` names the loopback default accepts (besides loopback IP literals and
#: ``*.localhost``, which RFC 6761 reserves for loopback).
LOCAL_HOST_NAMES = frozenset({"localhost"})
_TESTCLIENT = "testclient"  # Starlette's TestClient reports this as the client address


class RedlineHTTPError(HTTPException):
    """An ``HTTPException`` with a machine-readable ``code`` for the error shape."""

    def __init__(
        self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.message = message


def _is_loopback(host: str | None) -> bool:
    """Whether a *client address* is loopback (``None`` is a unix socket)."""
    if host is None:
        return True
    if host in ("localhost", _TESTCLIENT):
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
    ``[::1]``), with any port – plus the names in ``extra``. A missing ``Host`` (HTTP/1.0,
    raw ASGI) is accepted, since a browser always sends one.
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


def loopback_guard(*, allowed_hosts: Iterable[str] = ()) -> Callable[[Request], Awaitable[None]]:
    """A dependency allowing only loopback clients that address a loopback ``Host``.

    Checking the ``Host`` header defeats **DNS rebinding**: a web page on
    ``evil.example`` that re-points its own DNS name at ``127.0.0.1`` can make your
    browser send requests to the local server, but they carry ``Host: evil.example``.
    ``allowed_hosts`` adds names (e.g. a ``/etc/hosts`` alias such as ``"devbox"``);
    ports are ignored.
    """
    extra = frozenset(h.strip().lower() for h in allowed_hosts)

    async def guard(request: Request) -> None:
        client = request.client.host if request.client else None
        if not _is_loopback(client):
            raise RedlineHTTPError(
                403,
                "forbidden",
                "Redline is only reachable from localhost. Pass "
                "install(app, dependencies=[Depends(your_auth)]) to expose it.",
            )
        if not is_local_host_header(request.headers.get("host"), extra):
            raise RedlineHTTPError(
                403,
                "forbidden",
                "Redline only answers requests addressed to localhost, 127.0.0.1 or [::1] "
                "(this request's Host header names another site, as in a DNS-rebinding "
                "attack). Use loopback_guard(allowed_hosts=[...]) to allow another name.",
            )

    return guard


#: The default guard: loopback clients, loopback ``Host`` names.
local_only = loopback_guard()
local_only.__name__ = "local_only"
local_only.__doc__ = "Allow only loopback clients addressing a loopback Host (the default)."


async def csrf_guard(request: Request) -> None:
    """Reject cross-site requests to the mutating endpoints (DELETE traces, POST explain).

    Browsers attach an ``Origin`` header to every cross-origin and every same-origin
    non-GET request, so it must match the host the request was addressed to. Requests
    without ``Origin`` (curl, scripts, server-to-server) cannot be forged by a web page and
    are allowed – they are still subject to the auth dependencies. ``Sec-Fetch-Site:
    cross-site`` is rejected too. No change to the SPA is needed.

    This check alone does not stop DNS rebinding (there ``Origin`` and ``Host`` agree);
    the ``Host`` check of :func:`local_only`, or real authentication, does.
    """
    if request.method in _SAFE:
        return
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise RedlineHTTPError(403, "csrf_failed", "Cross-site request blocked.")
    origin = request.headers.get("origin")
    if origin is None:
        return
    allowed = {
        h.strip().lower()
        for h in (
            request.headers.get("host"),
            request.headers.get("x-forwarded-host"),
        )
        if h
    }
    netloc = urlsplit(origin).netloc.lower() if origin != "null" else ""
    if netloc not in allowed:
        raise RedlineHTTPError(403, "csrf_failed", "Cross-origin request blocked.")
