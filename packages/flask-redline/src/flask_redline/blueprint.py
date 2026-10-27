"""The UI + JSON API blueprint mounted under ``url_prefix``."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from flask import Blueprint, Response, request
from werkzeug.exceptions import HTTPException

from redline_core import stream, ui
from redline_core.api import RawResponse
from redline_core.storage import FilterError

from ._security import request_info

__all__ = ["build_blueprint", "error_response"]

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
) -> Response:
    """A JSON error in the API's shape (``CONTRACT.md`` §5)."""
    return json_response(
        {"error": {"code": code, "message": message, **(extra or {})}},
        status,
        headers={**(headers or {})},
    )


def json_response(payload: Any, status: int = 200, headers: dict[str, str] | None = None):
    return Response(
        json.dumps(payload, default=str),
        status=status,
        mimetype="application/json",
        headers={**_NO_STORE, **_SECURITY, **(headers or {})},
    )


def _respond(result: Any) -> Response:
    """Turn ``RedlineAPI.dispatch``'s result into a response.

    Usually ``(status, payload)`` with a JSON payload; the HTML snapshot comes back as a
    ``RawResponse(status, content_type, body, headers)``, sent as-is (plus the security
    headers, ``X-Frame-Options: DENY`` included).
    """
    if isinstance(result, tuple):
        status, payload = result
        if not isinstance(payload, RawResponse):
            return json_response(payload, status)
        result = payload
    if isinstance(result, RawResponse):
        return Response(
            result.body,
            status=result.status,
            content_type=result.content_type,
            headers={**_NO_STORE, **_SECURITY, **dict(getattr(result, "headers", None) or {})},
        )
    raise TypeError(f"unexpected dispatch result: {type(result).__name__}")  # pragma: no cover


def _http_error(exc: HTTPException) -> Response:
    """An ``HTTPException`` from the user's ``permission`` in the API's error shape."""
    status = exc.code or 500
    code = _CODES.get(status, "http_error")
    headers = {k: v for k, v in exc.get_headers() if k.lower() == "www-authenticate"}
    return error_response(status, code, exc.description or code, headers)


def build_blueprint(runtime: Any, url_prefix: str) -> Blueprint:
    bp = Blueprint("redline", __name__, url_prefix=url_prefix)

    @bp.before_request
    def guard() -> Response | None:
        """Every Redline URL: the core access check (``CONTRACT.md`` §18)."""
        permission: Callable[[Any], Any] | None = runtime.permission
        info = request_info(request, url_prefix)
        try:
            decision = runtime.access.check(
                info, framework_ok=(lambda: permission(request)) if permission else None
            )
        except HTTPException as exc:  # e.g. abort(401) inside a permission callable
            return _http_error(exc)
        if not decision.allowed:
            message = decision.message
            refusal = getattr(permission, "refusal", None)
            if decision.code == "forbidden" and runtime.access.policy == "framework":
                message = (refusal(request) if refusal else None) or (
                    "Redline refused this request (the permission check returned False)."
                )
            return error_response(
                decision.status,
                decision.code,
                message,
                dict(decision.headers),
                dict(decision.extra),
            )
        if info.path.strip("/") in ("api/login", "api/logout"):
            handled = runtime.access.handle(info, request.get_data())
            if handled is not None:  # POST api/login, api/logout
                return json_response(handled.body(), handled.status, headers=dict(handled.headers))
        return None

    @bp.get("/")
    def index() -> Response:
        return Response(ui.index_html(), mimetype="text/html", headers={**_NO_STORE, **_SECURITY})

    @bp.get("/assets/<path:path>")
    def assets(path: str) -> Response:
        file = ui.asset_path(f"assets/{path}")
        if file is None:
            return error_response(404, "not_found", "No such asset.")
        return _file(file, _IMMUTABLE)

    @bp.get("/api/stream")
    def api_stream() -> Response:
        """Live tail (SSE). Reads only Redline's own store, one short query per poll; no
        request/app context is kept alive while streaming (so no app DB session either)."""
        try:
            since = stream.parse_since(request.args)
        except FilterError as exc:
            return error_response(400, "invalid_filter", str(exc))
        return Response(
            stream.sync_events(
                runtime.api, since, stream.parse_after(request.args, request.headers)
            ),
            status=200,
            content_type=stream.CONTENT_TYPE,
            headers={**_SECURITY, **stream.SSE_HEADERS},
            direct_passthrough=True,
        )

    @bp.route("/api/<path:path>", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
    def api(path: str) -> Response:
        runtime.discover_engines()
        params: dict[str, Any] = {}
        for key, values in request.args.lists():
            params[key] = values[0] if len(values) == 1 else values
        body = request.get_data() if request.method == "POST" else None
        # PUT/PATCH reach the core, which answers 405 `method_not_allowed`.
        base_url = request.root_url.rstrip("/")  # the app's own origin + script root
        return _respond(
            runtime.api.dispatch(request.method, f"api/{path}", params, body, base_url=base_url)
        )

    @bp.get("/<path:name>")
    def top_level(name: str) -> Response:
        file = ui.asset_path(name)  # favicon.svg etc.
        if file is None:
            return error_response(404, "not_found", "Not found.")
        return _file(file, _NO_STORE)

    return bp


def _file(file: Any, cache: dict[str, str]) -> Response:
    return Response(
        file.read_bytes(),
        content_type=ui.content_type(file),
        headers={**cache, **_SECURITY},
    )
