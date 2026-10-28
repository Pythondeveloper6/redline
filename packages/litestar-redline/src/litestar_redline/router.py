"""The UI + JSON API router mounted under ``path``."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from litestar import Request, Response, Router, get, route
from litestar.concurrency import sync_to_thread
from litestar.enums import MediaType
from litestar.exceptions import HTTPException
from litestar.response import Redirect, Stream

from redline_core import stream, ui
from redline_core.api import MAX_API_BODY, RawResponse, own_base_url
from redline_core.storage import FilterError

from ._security import RedlineHTTPError, request_info

__all__ = ["build_router", "error_response"]

# ``request_max_body_size`` exists from Litestar 2.13 on (older versions have no limit).
_BODY_LIMIT: dict[str, Any] = (
    {"request_max_body_size": MAX_API_BODY}
    if "request_max_body_size" in inspect.signature(route).parameters
    else {}
)
_NO_STORE = {"Cache-Control": "no-store"}
_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}
_SECURITY = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    429: "too_many_requests",
}


def error_response(
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> Response[Any]:
    """A JSON error in the API's shape (``CONTRACT.md`` section 5)."""
    return Response(
        {"error": {"code": code, "message": message, **(extra or {})}},
        status_code=status,
        media_type=MediaType.JSON,
        headers={**_NO_STORE, **_SECURITY, **(headers or {})},
    )


def _http_exception_handler(_request: Request, exc: HTTPException) -> Response[Any]:
    """Render ``HTTPException`` - from Redline's guards *and* your own guards - as
    ``{"error": {"code", "message"}}`` so the UI can show it. Headers such as
    ``WWW-Authenticate`` are kept."""
    extra: dict[str, Any] = {}
    if isinstance(exc, RedlineHTTPError):
        code, message, extra = exc.code, exc.message, exc.extra
    else:
        code = _CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else code
    return error_response(exc.status_code, code, message, dict(exc.headers or {}), extra)


def _rest(request: Request[Any, Any, Any], name: str) -> str:
    """The ``:path`` parameter, read from the scope (no signature-inferred path params)."""
    return str(request.path_params.get(name, "")).lstrip("/")


def _file_response(file: Path, headers: dict[str, str]) -> Response[Any]:
    return Response(
        file.read_bytes(),
        media_type=ui.content_type(file),
        headers={**headers, **_SECURITY},
    )


def _params(request: Request[Any, Any, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    query = request.scope.get("query_string", b"").decode("latin-1")
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key in params:
            prev = params[key]
            params[key] = [*prev, value] if isinstance(prev, list) else [prev, value]
        else:
            params[key] = value
    return params


def base_url(request: Request[Any, Any, Any]) -> str | None:
    """The application's own root URL from the server's listening port (``scope["server"]``,
    never the ``Host`` header) + ``root_path``: the only target ``POST api/runs`` can load."""
    scope = request.scope
    return own_base_url(scope.get("scheme"), scope.get("server"), scope.get("root_path", ""))


def build_router(plugin: Any, path: str, guards: Sequence[Any]) -> Router:
    @get("/", include_in_schema=False)
    async def index(request: Request[Any, Any, Any]) -> Response[Any] | Redirect:
        raw = (request.scope.get("raw_path") or request.scope["path"].encode()).decode("latin-1")
        if not raw.split("?", 1)[0].endswith("/"):
            # Litestar serves /_redline too, but the UI's relative URLs need the slash
            query = request.scope.get("query_string", b"").decode("latin-1")
            target = f"{request.scope.get('root_path', '')}{path}/" + (f"?{query}" if query else "")
            return Redirect(target, status_code=307, headers=_SECURITY)
        return Response(
            ui.index_html(),
            media_type=MediaType.HTML,
            headers={**_NO_STORE, **_SECURITY},
        )

    @get("/assets/{asset:path}", include_in_schema=False)
    async def assets(request: Request[Any, Any, Any]) -> Response[Any]:
        file = ui.asset_path(f"assets/{_rest(request, 'asset')}")
        if file is None:
            return error_response(404, "not_found", "No such asset.")
        return _file_response(file, _IMMUTABLE)

    @get("/api/stream", include_in_schema=False)
    async def api_stream(request: Request[Any, Any, Any]) -> Response[Any]:
        # Server-sent events (live tail). Litestar's Stream response cancels the generator
        # when the client disconnects; store reads run in a worker thread.
        try:
            since = stream.parse_since(_params(request))
        except FilterError as exc:
            return error_response(400, "invalid_filter", str(exc))
        return Stream(
            stream.async_events(
                plugin.api, since, after=stream.parse_after(_params(request), request.headers)
            ),
            media_type=stream.CONTENT_TYPE,
            headers={**_SECURITY, **stream.SSE_HEADERS},
        )

    @route(
        "/api/{rest:path}",
        http_method=["GET", "POST", "DELETE", "PUT", "PATCH"],  # core answers 405 to PATCH
        status_code=200,
        include_in_schema=False,
        # imported check baselines may be up to 10 MiB (the core enforces it); Litestar's
        # default limit (since 2.13) is 10 MB
        **_BODY_LIMIT,
    )
    async def api(request: Request[Any, Any, Any]) -> Response[Any]:
        plugin.loop = asyncio.get_running_loop()
        if _rest(request, "rest").strip("/") in ("login", "logout"):  # the gate's own endpoints
            body = await request.body() if request.method == "POST" else None
            decision = plugin.access.handle(request_info(request, path), body)
            if decision is not None:
                return Response(
                    decision.body(),
                    status_code=decision.status,
                    media_type=MediaType.JSON,
                    headers={**_NO_STORE, **_SECURITY, **decision.headers},
                )
        params = _params(request)
        body = await request.body() if request.method in ("POST", "PUT") else None
        status, payload = await sync_to_thread(
            partial(
                plugin.api.dispatch,
                request.method,
                f"api/{_rest(request, 'rest')}",
                params,
                body,
                base_url=base_url(request),
            )
        )
        if isinstance(payload, RawResponse):  # the snapshot page
            return Response(
                payload.body,
                status_code=payload.status,
                media_type=payload.content_type,
                headers={**_NO_STORE, **payload.headers, **_SECURITY},
            )
        return Response(
            payload,
            status_code=status,
            media_type=MediaType.JSON,
            headers={**_NO_STORE, **_SECURITY},
        )

    @get("/{name:path}", include_in_schema=False)
    async def top_level(request: Request[Any, Any, Any]) -> Response[Any]:
        file = ui.asset_path(_rest(request, "name"))  # badge.js, favicon.svg etc.
        if file is None:
            return error_response(404, "not_found", "Not found.")
        return _file_response(file, _NO_STORE)

    return Router(
        path=path,
        route_handlers=[index, assets, api_stream, api, top_level],
        guards=list(guards),
        exception_handlers={HTTPException: _http_exception_handler},
        include_in_schema=False,
    )
