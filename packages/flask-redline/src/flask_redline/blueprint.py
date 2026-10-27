"""The UI + JSON API blueprint mounted under ``url_prefix``."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from flask import Blueprint, Response, request
from werkzeug.exceptions import HTTPException

from redline_core import ui

from ._security import RedlineHTTPError, check_csrf

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
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> Response:
    """A JSON error in the API's shape (``CONTRACT.md`` §5)."""
    return json_response(
        {"error": {"code": code, "message": message}}, status, headers={**(headers or {})}
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

    Today that is ``(status, payload)`` with a JSON payload. CONTRACT §12 lets ``dispatch``
    return a ``RawResponse(status, content_type, body, headers)`` instead (the HTML
    snapshot); this is the hook where it is sent as-is.
    """
    if isinstance(result, tuple):
        status, payload = result
        return json_response(payload, status)
    if hasattr(result, "content_type") and hasattr(result, "body"):
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
        permission: Callable[[Any], Any] = runtime.permission
        try:
            if not permission(request):
                refusal = getattr(permission, "refusal", None)
                message = (refusal(request) if refusal else None) or (
                    "Redline refused this request (the permission check returned False)."
                )
                raise RedlineHTTPError(403, "forbidden", message)
            if request.endpoint == "redline.api":
                check_csrf(request)
        except RedlineHTTPError as exc:
            return error_response(exc.status_code, exc.code, exc.message, exc.headers)
        except HTTPException as exc:  # e.g. abort(401) inside a permission callable
            return _http_error(exc)
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

    @bp.route("/api/<path:path>", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
    def api(path: str) -> Response:
        runtime.discover_engines()
        params: dict[str, Any] = {}
        for key, values in request.args.lists():
            params[key] = values[0] if len(values) == 1 else values
        body = request.get_data() if request.method == "POST" else None
        # PUT/PATCH reach the core, which answers 405 `method_not_allowed`.
        return _respond(runtime.api.dispatch(request.method, f"api/{path}", params, body))

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
