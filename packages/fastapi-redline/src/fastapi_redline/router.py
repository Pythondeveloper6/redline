"""The UI + JSON API router mounted under ``prefix``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute

from redline_core import stream, ui
from redline_core.api import RawResponse, own_base_url
from redline_core.storage import FilterError

from ._security import RedlineHTTPError, request_info

__all__ = ["RedlineRoute", "build_router", "error_response"]

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
) -> JSONResponse:
    """A JSON error in the API's shape (``CONTRACT.md`` §5)."""
    return JSONResponse(
        {"error": {"code": code, "message": message, **(extra or {})}},
        status_code=status,
        headers={**_NO_STORE, **_SECURITY, **(headers or {})},
    )


class RedlineRoute(APIRoute):
    """Renders ``HTTPException`` – from Redline's guards *and* your auth dependencies –
    as ``{"error": {"code", "message"}}`` instead of FastAPI's ``{"detail": ...}``, so
    the UI can show it. Headers such as ``WWW-Authenticate`` are kept."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except HTTPException as exc:
                extra: dict[str, Any] = {}
                if isinstance(exc, RedlineHTTPError):
                    code, message, extra = exc.code, exc.message, exc.extra
                else:
                    code = _CODES.get(exc.status_code, "http_error")
                    detail = exc.detail
                    message = detail if isinstance(detail, str) else str(detail or code)
                return error_response(
                    exc.status_code, code, message, dict(exc.headers or {}), extra
                )

        return route_handler


def build_router(redline: Any, dependencies: Sequence[Any]) -> APIRouter:
    router = APIRouter(
        dependencies=list(dependencies), include_in_schema=False, route_class=RedlineRoute
    )

    @router.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(ui.index_html(), headers={**_NO_STORE, **_SECURITY})

    @router.get("/assets/{path:path}")
    async def assets(path: str) -> Response:
        file = ui.asset_path(f"assets/{path}")
        if file is None:
            return error_response(404, "not_found", "No such asset.")
        return FileResponse(
            file, media_type=ui.content_type(file), headers={**_IMMUTABLE, **_SECURITY}
        )

    @router.get("/api/stream")
    async def live(request: Request) -> Response:
        """Server-sent events: one ``trace`` event per new trace (``CONTRACT.md`` §12)."""
        try:
            since = stream.parse_since(request.query_params)
        except FilterError as exc:
            return error_response(400, "invalid_filter", str(exc))
        return StreamingResponse(
            stream.async_events(
                redline.api,
                since,
                request.is_disconnected,
                after=stream.parse_after(request.query_params, request.headers),
            ),
            media_type=stream.CONTENT_TYPE,
            headers=stream.SSE_HEADERS,
        )

    @router.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "DELETE", "PUT", "PATCH"],  # the core answers 405 for the last two
    )
    async def api(request: Request, path: str) -> Response:
        redline.loop = asyncio.get_running_loop()
        if path.strip("/") in ("login", "logout"):  # the access gate's own endpoints
            body = await request.body() if request.method == "POST" else None
            decision = redline.access.handle(request_info(request, redline.prefix), body)
            if decision is not None:
                return JSONResponse(
                    decision.body(),
                    status_code=decision.status,
                    headers={**_NO_STORE, **_SECURITY, **decision.headers},
                )
        params: dict[str, Any] = {}
        for key, value in request.query_params.multi_items():
            if key in params:
                prev = params[key]
                params[key] = [*prev, value] if isinstance(prev, list) else [prev, value]
            else:
                params[key] = value
        body = await request.body() if request.method == "POST" else None
        # the server's own listening port, never the Host header (CONTRACT.md §18)
        scope = request.scope
        base_url = own_base_url(
            scope.get("scheme"), scope.get("server"), scope.get("root_path", "")
        )
        status, payload = await run_in_threadpool(
            redline.api.dispatch, request.method, f"api/{path}", params, body, base_url=base_url
        )
        if isinstance(payload, RawResponse):  # the snapshot page
            return Response(
                payload.body,
                status_code=payload.status,
                media_type=payload.content_type,
                headers={**payload.headers, **_NO_STORE, **_SECURITY},
            )
        return JSONResponse(payload, status_code=status, headers={**_NO_STORE, **_SECURITY})

    @router.get("/{name}")
    async def top_level(name: str) -> Response:
        file = ui.asset_path(name)  # badge.js, favicon.svg etc.
        if file is None:
            return error_response(404, "not_found", "Not found.")
        return FileResponse(
            file, media_type=ui.content_type(file), headers={**_NO_STORE, **_SECURITY}
        )

    return router
