"""The UI and the JSON API, mounted with ``path("redline/", include("django_redline.urls"))``.

Access
    Every view first checks ``REDLINE["PERMISSION"]`` (default: active staff users). The
    page redirects anonymous visitors to the admin login (or ``LOGIN_URL``) and answers
    403 to signed-in users without access; the API answers ``403`` JSON
    ``{"error": {"code": "forbidden", ...}}``.

CSRF
    The API runs Django's own CSRF check on every request, whether or not
    ``CsrfViewMiddleware`` is installed: ``GET`` is free, mutating requests (``DELETE``
    of traces, ``POST api/explain``) need the token in the CSRF header
    (``X-CSRFToken`` by default) – plus Django's Origin/Referer checks on HTTPS. A
    failure is a JSON ``403`` with code ``csrf_failed``. The page
    embeds a fresh token in ``<meta name="redline-csrf">`` (so it works with
    ``CSRF_COOKIE_HTTPONLY`` and ``CSRF_USE_SESSIONS``) and the SPA sends it back.

Live tail
    ``GET api/stream?since=<epoch>`` is a server-sent-event stream (same guard as the API):
    a sync generator under WSGI, an async one when the request came through ASGI (so it
    never pins a worker thread while it sleeps between polls).

Badge
    Top-level bundle files (``badge.js``, ``favicon.svg``) are served without the
    permission check: they hold no data, and the badge is injected into the app's own
    pages for whoever is browsing them in ``DEBUG``. The API the badge calls is guarded.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    HttpResponseForbidden,
    JsonResponse,
    StreamingHttpResponse,
)
from django.middleware.csrf import CsrfViewMiddleware, get_token
from django.urls import NoReverseMatch, get_script_prefix, reverse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from redline_core import stream, ui
from redline_core.api import RawResponse
from redline_core.storage import FilterError

from .runtime import get_runtime

__all__ = ["api", "asset", "index", "top_level"]

_HASHED = re.compile(r"-[A-Za-z0-9_-]{8,}\.(?:js|css|mjs)$")
_API_METHODS = ("GET", "POST", "DELETE")


def _allowed(request: HttpRequest) -> bool:
    try:
        return bool(get_runtime().options.permission(request))
    except Exception:
        return False


def _check_host(request: HttpRequest) -> None:
    """Enforce ``ALLOWED_HOSTS`` on Redline's views (``DisallowedHost`` -> 400).

    Django only validates ``Host`` when something calls ``get_host()``
    (``CommonMiddleware`` does); calling it here makes the protection against DNS
    rebinding independent of the middleware stack.
    """
    request.get_host()


def _login_url() -> str:
    try:
        return reverse("admin:login")
    except NoReverseMatch:
        return str(settings.LOGIN_URL)


def _page_guard(view: Callable[..., HttpResponseBase]) -> Callable[..., HttpResponseBase]:
    def guarded(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        _check_host(request)
        if _allowed(request):
            return view(request, *args, **kwargs)
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return redirect_to_login(request.get_full_path(), _login_url())
        return HttpResponseForbidden("You do not have access to Redline.")

    guarded.__name__ = view.__name__
    guarded.__doc__ = view.__doc__
    return guarded


def _csrf_header_name() -> str:
    meta = str(getattr(settings, "CSRF_HEADER_NAME", "HTTP_X_CSRFTOKEN"))
    name = meta[5:] if meta.startswith("HTTP_") else meta
    return "-".join(part.capitalize() for part in name.split("_")).replace("Csrftoken", "CSRFToken")


@ensure_csrf_cookie
@_page_guard
def index(request: HttpRequest) -> HttpResponse:
    """The single-page app (hash-routed, so one URL serves every screen)."""
    token = html.escape(get_token(request), quote=True)
    header = html.escape(_csrf_header_name(), quote=True)
    meta = f'<meta name="redline-csrf" content="{token}" data-header="{header}">'
    page = ui.index_html()
    page = page.replace("</head>", f"    {meta}\n  </head>", 1)
    response = HttpResponse(page, content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "same-origin"
    response["X-Frame-Options"] = "DENY"
    return response


@_page_guard
def asset(request: HttpRequest, path: str) -> FileResponse:
    """A file of the built UI bundle; hashed names are cached for a year."""
    file = ui.asset_path(f"assets/{path}")
    if file is None:
        raise Http404("No such Redline asset.")
    response = FileResponse(file.open("rb"), content_type=ui.content_type(file))
    if _HASHED.search(file.name):
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response["Cache-Control"] = "public, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "DENY"
    return response


def top_level(request: HttpRequest, name: str) -> HttpResponseBase:
    """A top-level file of the bundle (``badge.js``, ``favicon.svg``); never an HTML page."""
    _check_host(request)
    file = None if name.endswith((".html", ".htm")) else ui.asset_path(name)
    if file is None:
        raise Http404("No such Redline file.")
    response = FileResponse(file.open("rb"), content_type=ui.content_type(file))
    response["Cache-Control"] = "public, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "DENY"
    return response


def _json(status: int, payload: Any) -> JsonResponse:
    response = JsonResponse(payload, status=status, safe=False, json_dumps_params={"default": str})
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "DENY"
    return response


def _raw(result: RawResponse) -> HttpResponse:
    """A non-JSON core answer (the snapshot page), sent as-is."""
    response = HttpResponse(result.body, status=result.status, content_type=result.content_type)
    for key, value in result.headers.items():
        response[key] = value
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "DENY"
    return response


def _base_url(request: HttpRequest) -> str:
    """The app's own root URL: ``scheme://host[:port]`` + script prefix, no trailing slash."""
    return f"{request.scheme}://{request.get_host()}{get_script_prefix()}".rstrip("/")


def _is_asgi(request: HttpRequest) -> bool:
    from django.core.handlers.asgi import ASGIRequest

    return isinstance(request, ASGIRequest)


def _stream(request: HttpRequest) -> HttpResponseBase:
    """``GET api/stream``: server-sent events with new trace summaries."""
    if request.method != "GET":
        return _json(405, {"error": {"code": "method_not_allowed", "message": "Not allowed."}})
    try:
        since = stream.parse_since(request.GET)
    except FilterError as exc:
        return _json(400, {"error": {"code": "invalid_filter", "message": str(exc)}})
    api_ = get_runtime().api
    events: Any
    if _is_asgi(request):
        events = stream.async_events(api_, since)
    else:
        events = stream.sync_events(api_, since)
    response = StreamingHttpResponse(events, content_type=stream.CONTENT_TYPE)
    for key, value in stream.SSE_HEADERS.items():
        response[key] = value
    return response


class _JsonCsrf(CsrfViewMiddleware):
    """Django's CSRF check, answering with the API's JSON error shape."""

    def _reject(self, request: HttpRequest, reason: str) -> JsonResponse:  # type: ignore[override]
        return _json(403, {"error": {"code": "csrf_failed", "message": f"CSRF: {reason}"}})


def _csrf_rejection(request: HttpRequest) -> HttpResponseBase | None:
    check = _JsonCsrf(_no_response)
    request.csrf_processing_done = False  # type: ignore[attr-defined]
    return check.process_request(request) or check.process_view(request, _dispatch, (), {})


def _no_response(request: HttpRequest) -> HttpResponse:  # pragma: no cover - never called
    return HttpResponse()


# ``csrf_exempt`` only stops the *global* middleware from answering with its HTML failure
# page; ``_csrf_rejection`` runs exactly the same check (token header, Origin/Referer)
# for every request and answers with JSON instead.
@csrf_exempt
@transaction.non_atomic_requests
def api(request: HttpRequest, path: str = "") -> HttpResponseBase:
    """Route ``api/<path>`` to the core :class:`~redline_core.api.RedlineAPI`."""
    _check_host(request)
    if not _allowed(request):
        return _json(
            403, {"error": {"code": "forbidden", "message": "You do not have access to Redline."}}
        )
    if request.method not in _API_METHODS:
        return _json(405, {"error": {"code": "method_not_allowed", "message": "Not allowed."}})
    rejection = _csrf_rejection(request)
    if rejection is not None:
        return rejection
    if path.strip("/") == "stream":
        return _stream(request)
    return _dispatch(request, path)


def _dispatch(request: HttpRequest, path: str) -> HttpResponseBase:
    params = {key: request.GET.getlist(key) for key in request.GET}
    body = request.body if request.method == "POST" else None
    result = get_runtime().api.dispatch(
        request.method or "GET",
        f"api/{path}",
        params=params,
        body=body,
        base_url=_base_url(request),
    )
    if isinstance(result, RawResponse):
        return _raw(result)
    status, payload = result
    return _json(status, payload)
