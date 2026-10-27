"""The UI + JSON API router mounted under ``path``."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from litestar import Request, Response, Router, get, route
from litestar.concurrency import sync_to_thread
from litestar.enums import MediaType
from litestar.exceptions import HTTPException
from litestar.response import Redirect

from redline_core import ui

from ._security import RedlineHTTPError, csrf_guard

__all__ = ["build_router", "error_response"]

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
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> Response[Any]:
    """A JSON error in the API's shape (``CONTRACT.md`` section 5)."""
    return Response(
        {"error": {"code": code, "message": message}},
        status_code=status,
        media_type=MediaType.JSON,
        headers={**_NO_STORE, **_SECURITY, **(headers or {})},
    )


def _http_exception_handler(_request: Request, exc: HTTPException) -> Response[Any]:
    """Render ``HTTPException`` - from Redline's guards *and* your own guards - as
    ``{"error": {"code", "message"}}`` so the UI can show it. Headers such as
    ``WWW-Authenticate`` are kept."""
    if isinstance(exc, RedlineHTTPError):
        code, message = exc.code, exc.message
    else:
        code = _CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else code
    return error_response(exc.status_code, code, message, dict(exc.headers or {}))


def _rest(request: Request[Any, Any, Any], name: str) -> str:
    """The ``:path`` parameter, read from the scope (no signature-inferred path params)."""
    return str(request.path_params.get(name, "")).lstrip("/")


def _file_response(file: Path, headers: dict[str, str]) -> Response[Any]:
    return Response(
        file.read_bytes(),
        media_type=ui.content_type(file),
        headers={**headers, **_SECURITY},
    )


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

    @route(
        "/api/{rest:path}",
        http_method=["GET", "POST", "DELETE", "PUT", "PATCH"],  # core answers 405 to the last two
        guards=[csrf_guard],
        status_code=200,
        include_in_schema=False,
    )
    async def api(request: Request[Any, Any, Any]) -> Response[Any]:
        plugin.loop = asyncio.get_running_loop()
        params: dict[str, Any] = {}
        query = request.scope.get("query_string", b"").decode("latin-1")
        for key, value in parse_qsl(query, keep_blank_values=True):
            if key in params:
                prev = params[key]
                params[key] = [*prev, value] if isinstance(prev, list) else [prev, value]
            else:
                params[key] = value
        body = await request.body() if request.method == "POST" else None
        status, payload = await sync_to_thread(
            plugin.api.dispatch, request.method, f"api/{_rest(request, 'rest')}", params, body
        )
        return Response(
            payload,
            status_code=status,
            media_type=MediaType.JSON,
            headers={**_NO_STORE, **_SECURITY},
        )

    @get("/{name:path}", include_in_schema=False)
    async def top_level(request: Request[Any, Any, Any]) -> Response[Any]:
        file = ui.asset_path(_rest(request, "name"))  # favicon.svg etc.
        if file is None:
            return error_response(404, "not_found", "Not found.")
        return _file_response(file, _NO_STORE)

    return Router(
        path=path,
        route_handlers=[index, assets, api, top_level],
        guards=list(guards),
        exception_handlers={HTTPException: _http_exception_handler},
        include_in_schema=False,
    )
