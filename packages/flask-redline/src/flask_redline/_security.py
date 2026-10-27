"""Access control for the Redline UI and API.

Every Redline request goes through the core access gate
(:class:`redline_core.access.Access`, ``CONTRACT.md`` §18), from the blueprint's
``before_request``: the ``access`` setting picks the policy (``local`` by default).
``Redline(permission=...)`` implies ``access="framework"``: your callable decides.

* :func:`local_only` / :func:`loopback_guard` – loopback clients addressing a loopback
  ``Host`` (against DNS rebinding), usable as an explicit ``permission``.
* :func:`check_csrf` – the Origin / ``Sec-Fetch-Site`` check (the gate applies it anyway).
* :class:`RedlineHTTPError` – what the checks raise; the Redline blueprint renders it (and
  any ``HTTPException`` from your own ``permission`` callable) in the API's error shape,
  ``{"error": {"code": ..., "message": ...}}``.

A ``permission`` is any ``callable(request) -> bool``; the guards here are such callables.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from redline_core.access import LOCAL_HOST_NAMES, RequestInfo, is_local_host, is_loopback

__all__ = [
    "LOCAL_HOST_NAMES",
    "RedlineHTTPError",
    "check_csrf",
    "is_local_host_header",
    "local_only",
    "loopback_guard",
    "request_info",
]

_SAFE = {"GET", "HEAD", "OPTIONS"}


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
    return is_loopback(host)


def is_local_host_header(header: str | None, extra: Iterable[str] = ()) -> bool:
    """Whether a ``Host`` header names this machine's loopback interface (see
    :func:`redline_core.access.is_local_host`)."""
    return is_local_host(header, extra)


def environ_info(environ: dict[str, Any]) -> RequestInfo:
    """A :class:`RequestInfo` from a WSGI environ (for the profile-flag check)."""
    headers = [
        (key[5:].replace("_", "-"), value)
        for key, value in environ.items()
        if key.startswith("HTTP_") and isinstance(value, str)
    ]
    return RequestInfo.build(
        method=environ.get("REQUEST_METHOD", "GET"),
        path="",
        client_ip=environ.get("REMOTE_ADDR"),
        headers=headers,
        scheme=environ.get("wsgi.url_scheme", "http"),
        mount=environ.get("SCRIPT_NAME", "") or "/",
    )


def request_info(request: Any, mount: str) -> RequestInfo:
    """The framework-neutral description of a Flask ``request`` under ``mount``."""
    full = request.path  # without the script root
    rel = full[len(mount) :] if full.startswith(mount) else full
    return RequestInfo.build(
        method=request.method,
        path=rel.lstrip("/"),
        client_ip=request.environ.get("REMOTE_ADDR"),
        headers=list(request.headers.items()),
        host=request.environ.get("HTTP_HOST"),
        scheme=request.scheme,
        mount=f"{request.script_root.rstrip('/')}{mount}",
    )


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
    if request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
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
