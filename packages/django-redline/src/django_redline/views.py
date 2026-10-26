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
)
from django.middleware.csrf import CsrfViewMiddleware, get_token
from django.urls import NoReverseMatch, reverse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from redline_core import ui

from .runtime import get_runtime

__all__ = ["api", "asset", "index"]

_HASHED = re.compile(r"-[A-Za-z0-9_-]{8,}\.(?:js|css|mjs)$")
_API_METHODS = ("GET", "POST", "DELETE")


def _allowed(request: HttpRequest) -> bool:
    try:
        return bool(get_runtime().options.permission(request))
    except Exception:
        return False


def _login_url() -> str:
    try:
        return reverse("admin:login")
    except NoReverseMatch:
        return str(settings.LOGIN_URL)


def _page_guard(view: Callable[..., HttpResponseBase]) -> Callable[..., HttpResponseBase]:
    def guarded(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
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
    return response


def _json(status: int, payload: Any) -> JsonResponse:
    response = JsonResponse(payload, status=status, safe=False, json_dumps_params={"default": str})
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
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
    if not _allowed(request):
        return _json(
            403, {"error": {"code": "forbidden", "message": "You do not have access to Redline."}}
        )
    if request.method not in _API_METHODS:
        return _json(405, {"error": {"code": "method_not_allowed", "message": "Not allowed."}})
    rejection = _csrf_rejection(request)
    if rejection is not None:
        return rejection
    return _dispatch(request, path)


def _dispatch(request: HttpRequest, path: str) -> JsonResponse:
    params = {key: request.GET.getlist(key) for key in request.GET}
    body = request.body if request.method == "POST" else None
    status, payload = get_runtime().api.dispatch(
        request.method or "GET", f"api/{path}", params=params, body=body
    )
    return _json(status, payload)
