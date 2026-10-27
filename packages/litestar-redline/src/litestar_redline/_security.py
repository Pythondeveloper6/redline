"""Access control for the Redline UI and API.

Every Redline route runs :func:`redline_guard` first: the core access gate
(:class:`redline_core.access.Access`, ``CONTRACT.md`` §18) with the policy of the
``access`` setting (``local`` by default). ``RedlinePlugin(guards=[...])`` implies
``access="framework"``: your guards decide (Litestar runs them after this one).

* :func:`local_only` / :func:`loopback_guard` – loopback clients addressing a loopback
  ``Host`` (against DNS rebinding), for explicit use as guards.
* :func:`csrf_guard` – the Origin / ``Sec-Fetch-Site`` check (the gate applies it anyway).
* :class:`RedlineHTTPError` – what the guards raise; the Redline router renders it (and
  any ``HTTPException`` from your own auth dependencies) in the API's error shape,
  ``{"error": {"code": ..., "message": ...}}``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

from litestar.connection import ASGIConnection
from litestar.exceptions import HTTPException
from litestar.handlers.base import BaseRouteHandler

from redline_core.access import (
    LOCAL_HOST_NAMES,
    Access,
    Decision,
    RequestInfo,
    is_local_host,
    is_loopback,
)

__all__ = [
    "LOCAL_HOST_NAMES",
    "RedlineHTTPError",
    "csrf_guard",
    "is_local_host_header",
    "local_only",
    "loopback_guard",
    "redline_guard",
    "request_info",
]

_Guard = Callable[[ASGIConnection, BaseRouteHandler], Awaitable[None]]
_SAFE = {"GET", "HEAD", "OPTIONS"}
_TESTCLIENT = "testclient"  # Litestar's TestClient reports this as the client address


class RedlineHTTPError(HTTPException):
    """An ``HTTPException`` with a machine-readable ``code`` for the error shape."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        extra: dict | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.message = message
        self.extra = dict(extra or {})

    @classmethod
    def from_decision(cls, decision: Decision) -> RedlineHTTPError:
        return cls(
            decision.status,
            decision.code,
            decision.message,
            dict(decision.headers) or None,
            dict(decision.extra),
        )


def _is_loopback(host: str | None) -> bool:
    """Whether a *client address* is loopback (``None`` is a unix socket)."""
    return host == _TESTCLIENT or is_loopback(host)


def is_local_host_header(header: str | None, extra: Iterable[str] = ()) -> bool:
    """Whether a ``Host`` header names this machine's loopback interface (see
    :func:`redline_core.access.is_local_host`)."""
    return is_local_host(header, extra)


def request_info(connection: ASGIConnection, mount: str) -> RequestInfo:
    """The framework-neutral description of a request under ``mount`` (e.g. ``/_redline``)."""
    scope = connection.scope
    root = scope.get("root_path", "") or ""
    path = scope.get("path", "")
    if root and path.startswith(root + "/"):
        path = path[len(root) :]
    rel = path[len(mount) :] if path.startswith(mount) else path
    host = connection.client.host if connection.client else None
    return RequestInfo.build(
        method=scope.get("method", "GET"),
        path=rel.lstrip("/"),
        client_ip="127.0.0.1" if host == _TESTCLIENT else host,
        headers=scope.get("headers", []),
        scheme=connection.url.scheme,
        mount=f"{root.rstrip('/')}{mount}",
    )


def redline_guard(access: Access, mount: str) -> _Guard:
    """The guard every Redline route runs first: the core access check."""

    async def guard(connection: ASGIConnection, _handler: BaseRouteHandler) -> None:
        decision = access.check(request_info(connection, mount), framework_ok=lambda: True)
        if not decision.allowed:
            raise RedlineHTTPError.from_decision(decision)

    return guard


def loopback_guard(*, allowed_hosts: Iterable[str] = ()) -> _Guard:
    """A guard allowing only loopback clients that address a loopback ``Host``.

    Checking the ``Host`` header defeats **DNS rebinding**: a web page on
    ``evil.example`` that re-points its own DNS name at ``127.0.0.1`` can make your
    browser send requests to the local server, but they carry ``Host: evil.example``.
    ``allowed_hosts`` adds names (e.g. a ``/etc/hosts`` alias such as ``"devbox"``);
    ports are ignored.
    """
    extra = frozenset(h.strip().lower() for h in allowed_hosts)

    async def guard(request: ASGIConnection, _handler: BaseRouteHandler) -> None:
        client = request.client.host if request.client else None
        if not _is_loopback(client):
            raise RedlineHTTPError(
                403,
                "forbidden",
                "Redline is only reachable from localhost. Pass "
                "RedlinePlugin(guards=[your_auth]) to expose it.",
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


async def csrf_guard(request: ASGIConnection, _handler: BaseRouteHandler) -> None:
    """Reject cross-site requests to the mutating endpoints (DELETE traces, POST explain).

    Browsers attach an ``Origin`` header to every cross-origin and every same-origin
    non-GET request, so it must match the host the request was addressed to. Requests
    without ``Origin`` (curl, scripts, server-to-server) cannot be forged by a web page and
    are allowed – they are still subject to the auth dependencies. ``Sec-Fetch-Site:
    cross-site`` is rejected too. No change to the SPA is needed.

    This check alone does not stop DNS rebinding (there ``Origin`` and ``Host`` agree);
    the ``Host`` check of :func:`local_only`, or real authentication, does.
    """
    if request.scope["method"] in _SAFE:
        return
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
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
