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
"""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.http import HttpRequest, HttpResponseBase
from django.urls import NoReverseMatch, reverse

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
    return profile_requested(
        request.META.get("QUERY_STRING", ""), request.headers.get("X-Redline-Profile")
    )


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
            async with SpanContext("middleware", "middleware"):
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
        except Exception:
            logger.exception("Redline failed to finish a trace")


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


def _route(request: HttpRequest) -> tuple[str | None, str | None]:
    match = getattr(request, "resolver_match", None)
    if match is None:
        return None, None
    route = getattr(match, "route", None)
    if route is not None:
        route = "/" + route.lstrip("^/") if not route.startswith("/") else route
    func = match.func
    target = getattr(func, "view_class", None) or getattr(func, "cls", None) or func
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
