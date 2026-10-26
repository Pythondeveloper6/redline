"""Access control for the Redline UI and API.

* :func:`local_only` – the default when ``install(dependencies=None)``: only loopback
  clients get in.
* :func:`csrf_guard` – always on for the API's mutating methods (see the function).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

__all__ = ["csrf_guard", "local_only"]

_SAFE = {"GET", "HEAD", "OPTIONS"}
_LOCAL_NAMES = {"localhost", "testclient"}  # `testclient` is Starlette's TestClient


def _is_loopback(host: str | None) -> bool:
    if host is None:  # unix socket
        return True
    if host in _LOCAL_NAMES:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


async def local_only(request: Request) -> None:
    """Allow only loopback clients (127.0.0.0/8, ::1, unix sockets)."""
    host = request.client.host if request.client else None
    if not _is_loopback(host):
        raise HTTPException(
            status_code=403,
            detail=(
                "Redline is only reachable from localhost. Pass "
                "install(app, dependencies=[Depends(your_auth)]) to expose it."
            ),
        )


async def csrf_guard(request: Request) -> None:
    """Reject cross-site requests to the mutating endpoints (DELETE traces, POST explain).

    Browsers attach an ``Origin`` header to every cross-origin and every same-origin
    non-GET request, so it must match the host the request was addressed to. Requests
    without ``Origin`` (curl, scripts, server-to-server) cannot be forged by a web page and
    are allowed – they are still subject to the auth dependencies. ``Sec-Fetch-Site:
    cross-site`` is rejected too. No change to the SPA is needed.
    """
    if request.method in _SAFE:
        return
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site request blocked.")
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
        raise HTTPException(status_code=403, detail="Cross-origin request blocked.")
