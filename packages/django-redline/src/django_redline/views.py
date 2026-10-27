"""The UI and the JSON API, mounted with ``path("redline/", include("django_redline.urls"))``.

Access
    Every view asks the core access gate (``redline_core.access``, ``CONTRACT.md`` §18).
    The default policy is ``access="framework"``: ``REDLINE["PERMISSION"]`` (default:
    active superusers). Then the page redirects anonymous visitors to the admin login
    (or ``LOGIN_URL``) and answers 403 to signed-in users without access; the API answers
    ``403`` JSON ``{"error": {"code": "forbidden", ...}}``. ``ACCESS = "token"``/``"basic"``
    /``"local"``/``"custom"`` switch to the core policies (with ``token``/``basic`` the
    page is served to anyone so the UI can show its login screen; the API answers 401
    ``login_required``).

CSRF
    The API runs Django's own CSRF check on every request, whether or not
    ``CsrfViewMiddleware`` is installed (except requests authenticated by an
    ``Authorization: Bearer`` header, which a browser never sends on its own): ``GET`` is
    free, mutating requests (``DELETE`` of traces, ``POST api/explain``, ``POST
    api/login``) need the token in the CSRF header
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
    ``PERMISSION`` check (the network rules of the gate still apply): they hold no data,
    and the badge is injected into the app's own pages for whoever is browsing them in
    ``DEBUG``. The API the badge calls is guarded.
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
    RawPostDataException,
    StreamingHttpResponse,
)
from django.middleware.csrf import CsrfViewMiddleware, get_token
from django.urls import NoReverseMatch, get_script_prefix, reverse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from redline_core import stream, ui
from redline_core.access import Decision, RequestInfo
from redline_core.api import RawResponse, own_base_url
from redline_core.storage import FilterError

from .runtime import get_runtime

__all__ = ["api", "asset", "index", "top_level"]

_HASHED = re.compile(r"-[A-Za-z0-9_-]{8,}\.(?:js|css|mjs)$")
_API_METHODS = ("GET", "POST", "DELETE")


def _permission(request: HttpRequest) -> bool:
    try:
        return bool(get_runtime().options.permission(request))
    except Exception:
        return False


def _info(request: HttpRequest, rel: str) -> RequestInfo:
    """The framework-neutral request for the gate; ``rel`` is the mount-relative path."""
    full = request.path
    mount = full[: len(full) - len(rel)] if rel and full.endswith(rel) else full
    return RequestInfo.build(
        method=request.method or "GET",
        path=rel,
        client_ip=request.META.get("REMOTE_ADDR"),
        headers=list(request.headers.items()),
        host=request.META.get("HTTP_HOST"),
        scheme=request.scheme or "http",
        mount=mount.rstrip("/") or "/",
    )


def _decide(request: HttpRequest, rel: str, *, permission: bool = True) -> Decision:
    hook = (lambda: _permission(request)) if permission else (lambda: True)
    return get_runtime().access.check(_info(request, rel), framework_ok=hook)


def _refusal(decision: Decision) -> JsonResponse:
    response = _json(decision.status, decision.body())
    for key, value in decision.headers.items():
        if key.lower() == "set-cookie":  # Django keeps cookies apart from the headers
            response.cookies.load(value)
        else:
            response[key] = value
    return response


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
        rel = f"assets/{kwargs['path']}" if "path" in kwargs else ""
        decision = _decide(request, rel)
        if decision.allowed:
            return view(request, *args, **kwargs)
        if decision.code != "forbidden" or get_runtime().access.policy != "framework":
            return _refusal(decision)
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
    decision = _decide(request, name, permission=False)
    if not decision.allowed:
        return _refusal(decision)
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


def _base_url(request: HttpRequest) -> str | None:
    """The app's own root URL from the server's listening port (``SERVER_PORT``, never the
    ``Host`` header) + script prefix, no trailing slash."""
    scope = getattr(request, "scope", None)
    scheme = scope.get("scheme") if isinstance(scope, dict) else None
    scheme = scheme or request.META.get("wsgi.url_scheme")
    return own_base_url(scheme, request.META.get("SERVER_PORT"), get_script_prefix())


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
    after = stream.parse_after(request.GET, request.headers)
    events: Any
    if _is_asgi(request):
        events = stream.async_events(api_, since, after=after)
    else:
        events = stream.sync_events(api_, since, after)
    response = StreamingHttpResponse(events, content_type=stream.CONTENT_TYPE)
    for key, value in stream.SSE_HEADERS.items():
        response[key] = value
    return response


class _JsonCsrf(CsrfViewMiddleware):
    """Django's CSRF check, answering with the API's JSON error shape."""

    def _reject(self, request: HttpRequest, reason: str) -> JsonResponse:  # type: ignore[override]
        return _json(403, {"error": {"code": "csrf_failed", "message": f"CSRF: {reason}"}})


def _csrf_rejection(request: HttpRequest) -> HttpResponseBase | None:
    access = get_runtime().access
    auth = request.headers.get("Authorization", "")
    if access.policy == "token" and auth[:7].lower() == "bearer ":
        return None  # a browser never attaches a Bearer token by itself: not forgeable
    check = _JsonCsrf(_no_response)
    request.csrf_processing_done = False  # type: ignore[attr-defined]
    return check.process_request(request) or check.process_view(request, _dispatch, (), {})


def _body(request: HttpRequest) -> bytes:
    """The raw body; empty when the CSRF check already consumed a form-encoded stream."""
    try:
        return request.body
    except RawPostDataException:
        return b""


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
    info = _info(request, f"api/{path}")
    decision = get_runtime().access.check(info, framework_ok=lambda: _permission(request))
    if not decision.allowed:
        return _refusal(decision)
    if request.method not in _API_METHODS:
        return _json(405, {"error": {"code": "method_not_allowed", "message": "Not allowed."}})
    rejection = _csrf_rejection(request)
    if rejection is not None:
        return rejection
    if path.strip("/") in ("login", "logout"):
        handled = get_runtime().access.handle(info, _body(request))
        if handled is not None:
            return _refusal(handled)  # status, JSON body and Set-Cookie
    if path.strip("/") == "stream":
        return _stream(request)
    return _dispatch(request, path)


def _dispatch(request: HttpRequest, path: str) -> HttpResponseBase:
    params = {key: request.GET.getlist(key) for key in request.GET}
    body = _body(request) if request.method == "POST" else None
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
