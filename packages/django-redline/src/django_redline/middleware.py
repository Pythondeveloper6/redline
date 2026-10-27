"""``RedlineMiddleware``: one trace per request, in sync (WSGI) and async (ASGI) stacks.

Put it near the top of ``MIDDLEWARE`` – after ``SecurityMiddleware`` and
``GZipMiddleware`` (so it sees uncompressed responses) and before everything else (so it
times the rest of the stack)::

    MIDDLEWARE = [
        "django.middleware.security.SecurityMiddleware",
        "django_redline.middleware.RedlineMiddleware",
        ...
    ]

What it records: method, path, the resolved route template and view, raw request headers,
query string and (small, non-multipart) body – redacted by the core before storage – the
response status, headers and body (never for streaming or file responses, which are left
untouched), unhandled exceptions (re-raised, never swallowed), the ``X-Redline-Run``
load-test tag, and a pyinstrument flamegraph when asked with ``?_redline=profile`` or
``X-Redline-Profile: 1``. Requests under Redline's own mount prefix are never recorded.

On every recorded response it adds a ``Server-Timing`` header (``server_timing`` setting;
appended to one the view set) and, when the ``badge`` setting is on (``"auto"`` = ``DEBUG``),
injects the floating badge ``<script>`` before ``</body>`` of a kept 2xx ``text/html``
``HttpResponse`` – never a streaming or file response, a compressed body, one larger than
2 MB or one whose ``Content-Security-Policy`` does not allow ``'self'`` scripts.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.urls import NoReverseMatch, reverse

from redline_core import ui
from redline_core.access import RequestInfo
from redline_core.blocking import track_request
from redline_core.recorder import SpanContext, TraceBuilder
from redline_core.sampling import profile_requested

from .instrument import begin_request, end_request
from .runtime import get_runtime

__all__ = ["RedlineMiddleware", "mount_prefix"]

logger = logging.getLogger("redline")

_NOT_RESOLVED = object()
_mount: Any = _NOT_RESOLVED


def mount_prefix() -> str | None:
    """The path Redline's URLs are mounted at (``/redline/``), or ``None``."""
    global _mount
    if _mount is _NOT_RESOLVED:
        try:
            _mount = reverse("redline:index")
        except NoReverseMatch:
            _mount = None
        except Exception:  # URLconf not importable yet; try again next request
            return None
    return _mount


def _reset_mount(**kwargs: Any) -> None:
    global _mount
    _mount = _NOT_RESOLVED


def _profile_requested(request: HttpRequest) -> bool:
    """``?_redline=profile`` / ``X-Redline-Profile: 1`` – honoured only with ``DEBUG`` or
    when the request would be let into Redline's API without ``PERMISSION`` (a token,
    Basic credentials, a login cookie…): anyone else could force profiling and storage."""
    if not profile_requested(
        request.META.get("QUERY_STRING", ""), request.headers.get("X-Redline-Profile")
    ):
        return False
    if settings.DEBUG:
        return True
    info = RequestInfo.build(
        method="GET",
        path="",
        client_ip=request.META.get("REMOTE_ADDR"),
        headers=list(request.headers.items()),
        host=request.META.get("HTTP_HOST"),
        scheme=request.scheme or "http",
    )
    return get_runtime().access.may_force(info)


class RedlineMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response
        self.async_mode = iscoroutinefunction(get_response)
        if self.async_mode:
            markcoroutinefunction(self)

    # entry points ------------------------------------------------------------
    def __call__(self, request: HttpRequest) -> Any:
        if self.async_mode:
            return self.__acall__(request)
        builder = self._start(request)
        if builder is None:
            return self.get_response(request)
        try:
            with SpanContext("middleware", "middleware"):
                response = self.get_response(request)
        except BaseException as exc:
            self._finish(builder, request, None, exc)
            raise
        self._finish(builder, request, response, None)
        return response

    async def __acall__(self, request: HttpRequest) -> Any:
        builder = self._start(request)
        if builder is None:
            return await self.get_response(request)
        try:
            async with track_request(builder), SpanContext("middleware", "middleware"):
                response = await self.get_response(request)
        except BaseException as exc:
            self._finish(builder, request, None, exc)
            raise
        self._finish(builder, request, response, None)
        return response

    def process_exception(self, request: HttpRequest, exception: BaseException) -> None:
        """Remember the view's exception for the trace; never handles it.

        It is only recorded as the trace's ``error`` when the final response is a 5xx:
        ``Http404``, ``PermissionDenied``, ``BadRequest``… become 4xx responses and are
        client errors, not failures (so ``always_keep_errors`` does not keep them).
        """
        request._redline_error = exception  # type: ignore[attr-defined]
        return None

    # helpers -----------------------------------------------------------------
    def _start(self, request: HttpRequest) -> TraceBuilder | None:
        try:
            rt = get_runtime()
            path = request.path
            prefix = mount_prefix()
            if prefix and path.startswith(prefix):
                return None
            recorder = rt.recorder
            if not recorder.should_record(path):
                return None
            body = None
            if rt.settings.capture_request_body:
                body = _small_body(request, rt.settings.max_body_bytes)
            builder = recorder.start_trace(
                framework="django",
                method=request.method or "GET",
                path=path,
                headers=dict(request.headers),
                query=request.META.get("QUERY_STRING", ""),
                body=body,
                client=request.META.get("REMOTE_ADDR"),
                profile_requested=_profile_requested(request),
            )
            request._redline_view_token = begin_request()  # type: ignore[attr-defined]
            return builder
        except Exception:  # Redline must never break the application
            logger.exception("Redline failed to start a trace")
            return None

    def _finish(
        self,
        builder: TraceBuilder,
        request: HttpRequest,
        response: HttpResponseBase | None,
        exc: BaseException | None,
    ) -> None:
        token = getattr(request, "_redline_view_token", None)
        if token is not None:
            end_request(token)
        try:
            route, view = _route(request)
            match = getattr(request, "resolver_match", None)
            if match is not None and match.kwargs:
                # ``/reset/<uidb64>/<token>/``: secret-looking values are masked in the path
                builder.set_route(None, None, match.kwargs)
            if builder.req_body is None and builder.recorder.settings.capture_request_body:
                builder.req_body = getattr(request, "_body", None)
            error = exc or getattr(request, "_redline_error", None)
            if response is not None and response.status_code < 500:
                error = None  # Http404 & co.: handled by Django as a 4xx, not a failure
            if response is None:
                builder.recorder.finish_trace(
                    builder, status=500, route=route, view=view, error=exc
                )
                return
            headers, body, size = _response_parts(response)
            rs = builder.recorder.settings
            on = ui.server_timing_enabled(rs, settings.DEBUG)
            timing = builder.server_timing() if on else ""
            snippet = _badge_snippet(builder, response)
            trace = builder.recorder.finish_trace(
                builder,
                status=response.status_code,
                headers=headers,
                body=body,
                size=size,
                route=route,
                view=view,
                error=error,
            )
            if trace.tags.get("sampled_by") and settings.DEBUG:
                response.headers["X-Redline-Trace"] = trace.id
            if timing:
                existing = response.get("Server-Timing")
                response.headers["Server-Timing"] = f"{existing}, {timing}" if existing else timing
            if snippet and trace.tags.get("sampled_by"):
                _inject(response, snippet)
        except Exception:
            logger.exception("Redline failed to finish a trace")


def _badge_snippet(builder: TraceBuilder, response: HttpResponseBase) -> str | None:
    """The badge ``<script>`` for ``response``, or ``None`` when it does not get one."""
    if not builder.active or not ui.badge_enabled(builder.recorder.settings, settings.DEBUG):
        return None
    # FileResponse / StreamingHttpResponse are not HttpResponse subclasses.
    if not isinstance(response, HttpResponse) or response.streaming:
        return None
    if not ui.badge_allowed(
        response.status_code,
        response.get("Content-Type"),
        content_encoding=response.get("Content-Encoding"),
        csp=response.get("Content-Security-Policy"),
        content_disposition=response.get("Content-Disposition"),
    ):
        return None
    if len(response.content) > ui.BADGE_MAX_BYTES:
        return None
    mount = mount_prefix()
    if not mount:
        return None
    return ui.badge_snippet(builder.id, mount, ui.badge_summary(builder, response.status_code))


def _inject(response: HttpResponseBase, snippet: str) -> None:
    new = ui.inject_badge(response.content, snippet)  # type: ignore[attr-defined]
    if new is None:
        return
    response.content = new  # type: ignore[attr-defined]
    if response.has_header("Content-Length"):
        response.headers["Content-Length"] = str(len(new))


def _small_body(request: HttpRequest, limit: int) -> bytes | None:
    """The request body when it is cheap and safe to read now; else ``None``.

    Multipart uploads and bodies larger than ``max_body_bytes`` are not read up front
    (the view may stream them); whatever the view itself read is picked up afterwards.
    """
    ctype = request.META.get("CONTENT_TYPE", "")
    if ctype.startswith("multipart/"):
        return None
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return None
    if length <= 0 or length > max(limit, 0):
        return None
    try:
        return request.body
    except Exception:
        return None


def _ninja_operation(func: Any, method: str | None) -> Any:
    """The endpoint function behind a Django Ninja route, else ``None``.

    Ninja registers one Django view per *path* (a closure over its ``PathView``, whose
    ``operations`` hold the real functions, one or more per HTTP method), so
    ``resolver_match.func`` is always ``ninja.operation...view_wrapper``. Nothing is
    imported: if Ninja is not already loaded, this is not a Ninja request.
    """
    operation_module = sys.modules.get("ninja.operation")
    path_view_cls = getattr(operation_module, "PathView", None)
    if path_view_cls is None or getattr(func, "__module__", None) != "ninja.operation":
        return None
    candidates = [getattr(func, "__self__", None)]  # older Ninja: a bound method
    for cell in getattr(func, "__closure__", None) or ():
        try:
            candidates.append(cell.cell_contents)
        except ValueError:  # an empty cell
            continue
    for candidate in candidates:
        if not isinstance(candidate, path_view_cls):
            continue
        for operation in candidate.operations:
            if method in operation.methods:
                return operation.view_func
        return None
    return None


def _route(request: HttpRequest) -> tuple[str | None, str | None]:
    match = getattr(request, "resolver_match", None)
    if match is None:
        return None, None
    route = getattr(match, "route", None)
    if route is not None:
        route = "/" + route.lstrip("^/") if not route.startswith("/") else route
    func = match.func
    target = _ninja_operation(func, request.method) or (
        getattr(func, "view_class", None) or getattr(func, "cls", None) or func
    )
    module = getattr(target, "__module__", None)
    name = getattr(target, "__qualname__", None) or getattr(target, "__name__", None)
    view = f"{module}.{name}" if module and name else getattr(match, "_func_path", None)
    return route, view


def _response_parts(response: HttpResponseBase) -> tuple[list[tuple[str, str]], Any, int | None]:
    headers = list(response.items())
    for morsel in response.cookies.values():
        headers.append(("Set-Cookie", morsel.OutputString()))
    size: int | None = None
    length = response.get("Content-Length")
    if length and length.isdigit():
        size = int(length)
    if getattr(response, "streaming", False):
        return headers, None, size  # never consume a streaming/file response
    try:
        content = response.content  # type: ignore[attr-defined]
    except Exception:
        return headers, None, size
    return headers, content, len(content)
