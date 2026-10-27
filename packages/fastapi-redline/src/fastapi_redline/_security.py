"""Access control for the Redline UI and API.

Every Redline request goes through :func:`redline_guard`, which asks the core access gate
(:class:`redline_core.access.Access`, ``CONTRACT.md`` §18) – the ``access`` setting picks
the policy (``local`` by default; ``token``, ``basic``, ``framework``, ``custom``,
``open``). Passing ``install(dependencies=[...])`` implies ``access="framework"``: your
dependencies decide, and FastAPI runs them as usual.

The helpers below are kept for explicit use as dependencies:

* :func:`local_only` / :func:`loopback_guard` – loopback clients addressing a loopback
  ``Host`` (against DNS rebinding);
* :func:`csrf_guard` – the Origin / ``Sec-Fetch-Site`` check (the gate applies it anyway);
* :class:`RedlineHTTPError` – what the guards raise; the Redline router renders it (and
  any ``HTTPException`` from your own auth dependencies) in the API's error shape,
  ``{"error": {"code": ..., "message": ...}}``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

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

_SAFE = {"GET", "HEAD", "OPTIONS"}
_TESTCLIENT = "testclient"  # Starlette's TestClient reports this as the client address


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


def _client_ip(request: Request) -> str | None:
    host = request.client.host if request.client else None
    return "127.0.0.1" if host == _TESTCLIENT else host


def _is_loopback(host: str | None) -> bool:
    """Whether a *client address* is loopback (``None`` is a unix socket)."""
    return host == _TESTCLIENT or is_loopback(host)


def is_local_host_header(header: str | None, extra: Iterable[str] = ()) -> bool:
    """Whether a ``Host`` header names this machine's loopback interface (see
    :func:`redline_core.access.is_local_host`)."""
    return is_local_host(header, extra)


def request_info(request: Request, mount: str) -> RequestInfo:
    """The framework-neutral description of a request under ``mount`` (e.g. ``/_redline``)."""
    root = request.scope.get("root_path", "") or ""
    path = request.scope.get("path", "")
    if root and path.startswith(root + "/"):
        path = path[len(root) :]
    rel = path[len(mount) :] if path.startswith(mount) else path
    return RequestInfo.build(
        method=request.method,
        path=rel.lstrip("/"),
        client_ip=_client_ip(request),
        headers=request.headers.items(),
        scheme=request.url.scheme,
        mount=f"{root.rstrip('/')}{mount}",
    )


def redline_guard(access: Access, mount: str) -> Callable[[Request], Awaitable[None]]:
    """The dependency every Redline route runs first: the core access check."""

    async def guard(request: Request) -> None:
        decision = access.check(request_info(request, mount), framework_ok=lambda: True)
        if not decision.allowed:
            raise RedlineHTTPError.from_decision(decision)

    guard.__name__ = "redline_guard"
    return guard


def loopback_guard(*, allowed_hosts: Iterable[str] = ()) -> Callable[[Request], Awaitable[None]]:
    """A dependency allowing only loopback clients that address a loopback ``Host``.

    Checking the ``Host`` header defeats **DNS rebinding**: a web page on
    ``evil.example`` that re-points its own DNS name at ``127.0.0.1`` can make your
    browser send requests to the local server, but they carry ``Host: evil.example``.
    ``allowed_hosts`` adds names (e.g. a ``/etc/hosts`` alias such as ``"devbox"``);
    ports are ignored. (The ``access="local"`` policy does the same with the
    ``allowed_hosts`` setting.)
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
        if not is_local_host(request.headers.get("host"), extra):
            raise RedlineHTTPError(
                403,
                "forbidden",
                "Redline only answers requests addressed to localhost, 127.0.0.1 or [::1] "
                "(this request's Host header names another site, as in a DNS-rebinding "
                "attack). Use loopback_guard(allowed_hosts=[...]) to allow another name.",
            )

    return guard


#: Loopback clients, loopback ``Host`` names (what ``access="local"`` enforces).
local_only = loopback_guard()
local_only.__name__ = "local_only"
local_only.__doc__ = "Allow only loopback clients addressing a loopback Host."


async def csrf_guard(request: Request) -> None:
    """Reject cross-site requests to the mutating endpoints.

    Browsers attach an ``Origin`` header to every cross-origin and every same-origin
    non-GET request, so it must match the host the request was addressed to. Requests
    without ``Origin`` (curl, scripts) cannot be forged by a web page and are allowed.
    ``Sec-Fetch-Site: cross-site`` is rejected too. The access gate applies the same rule
    to every Redline request.
    """
    if request.method in _SAFE:
        return
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        raise RedlineHTTPError(403, "csrf_failed", "Cross-site request blocked.")
    origin = request.headers.get("origin")
    if origin is None:
        return
    allowed = {h.strip().lower() for h in (request.headers.get("host"),) if h}
    netloc = urlsplit(origin).netloc.lower() if origin != "null" else ""
    if netloc not in allowed:
        raise RedlineHTTPError(403, "csrf_failed", "Cross-origin request blocked.")
