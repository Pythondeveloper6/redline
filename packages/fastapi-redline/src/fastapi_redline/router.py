"""The UI + JSON API router mounted under ``prefix``."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from redline_core import ui

from ._security import csrf_guard

__all__ = ["build_router"]

_NO_STORE = {"Cache-Control": "no-store"}
_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


def build_router(redline: Any, dependencies: Sequence[Any]) -> APIRouter:
    router = APIRouter(dependencies=list(dependencies), include_in_schema=False)

    @router.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(ui.index_html(), headers=_NO_STORE)

    @router.get("/assets/{path:path}")
    async def assets(path: str) -> Response:
        file = ui.asset_path(f"assets/{path}")
        if file is None:
            return JSONResponse(
                {"error": {"code": "not_found", "message": "No such asset."}}, status_code=404
            )
        return FileResponse(file, media_type=ui.content_type(file), headers=_IMMUTABLE)

    @router.api_route(
        "/api/{path:path}", methods=["GET", "POST", "DELETE"], dependencies=[Depends(csrf_guard)]
    )
    async def api(request: Request, path: str) -> JSONResponse:
        redline.loop = asyncio.get_running_loop()
        params: dict[str, Any] = {}
        for key, value in request.query_params.multi_items():
            if key in params:
                prev = params[key]
                params[key] = [*prev, value] if isinstance(prev, list) else [prev, value]
            else:
                params[key] = value
        body = await request.body() if request.method == "POST" else None
        status, payload = await run_in_threadpool(
            redline.api.dispatch, request.method, f"api/{path}", params, body
        )
        return JSONResponse(payload, status_code=status, headers=_NO_STORE)

    @router.get("/{name}")
    async def top_level(name: str) -> Response:
        file = ui.asset_path(name)  # favicon.svg etc.
        if file is None:
            return JSONResponse(
                {"error": {"code": "not_found", "message": "Not found."}}, status_code=404
            )
        return FileResponse(file, media_type=ui.content_type(file), headers=_NO_STORE)

    return router
