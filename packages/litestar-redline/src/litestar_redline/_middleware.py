"""The pure-ASGI recording middleware.

Not ``BaseHTTPMiddleware``: that wraps the app in extra tasks, breaks contextvars
propagation for sync endpoints and buffers streaming responses. This one:

* *tees* the request body as the app reads it (the app sees the untouched ``receive``);
* wraps ``send`` to capture status, headers and a size-capped copy of the response body –
  a stream is never buffered beyond ``max_body_bytes``;
* finishes the trace right after the last body chunk is sent, so background tasks do not
  count towards latency;
* resolves the route template (``/orders/{order_id:int}``) from ``scope["path_template"]``
  once routing ran;
* adds a ``Server-Timing`` header (``settings.server_timing``) to ``http.response.start``,
  appended to one the app already set;
* injects the floating badge into small, non-streamed ``text/html`` 2xx pages when
  :func:`redline_core.ui.badge_enabled` (``auto`` = ``app.debug``): the start message is
  held back only when ``content-length`` is known and at most ``BADGE_MAX_BYTES``, or when
  the first body message is the whole body; anything else passes through untouched;
* runs the app inside :func:`redline_core.blocking.track_request` (event-loop lag monitor).

It is installed as a Litestar *route-level* middleware, which sits outside the per-route
exception handler: an exception is already a response when it gets here, so the plugin's
``after_exception`` hook parks it in ``scope["redline.exception"]`` for ``finalize``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from redline_core import Recorder, get_recorder, ui
from redline_core.blocking import track_request
from redline_core.sampling import profile_requested

__all__ = ["EXCEPTION_KEY", "RedlineMiddleware"]

#: Scope key the plugin's ``after_exception`` hook stores the raised exception under.
EXCEPTION_KEY = "redline.exception"

_JSON_PAIR = re.compile(r'("(?:[^"\\]|\\.)*")(\s*:\s*)("(?:[^"\\]|\\.)*"|[^,}\]\s]+)')


def _wants_profile(headers: list[tuple[bytes, bytes]], query: bytes) -> bool:
    header = next((v for k, v in headers if k == b"x-redline-profile"), None)
    return profile_requested(query, header)


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _endpoint_name(endpoint: Any) -> str | None:
    if endpoint is None:
        return None
    target = getattr(endpoint, "func", endpoint)  # functools.partial
    module = getattr(target, "__module__", None)
    name = getattr(target, "__qualname__", None) or getattr(target, "__name__", None)
    if name is None:
        name = type(target).__qualname__
        module = type(target).__module__
    return f"{module}.{name}" if module else name


class _Capture:
    """A byte buffer that keeps at most ``limit + 1`` bytes (the +1 lets core flag truncation)."""

    __slots__ = ("buf", "limit", "total")

    def __init__(self, limit: int) -> None:
        self.buf = bytearray()
        self.limit = limit
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.limit + 1 - len(self.buf)
        if room > 0 and chunk:
            self.buf += chunk[:room]

    def value(self, recorder: Recorder, content_type: str | None) -> bytes:
        data = bytes(self.buf)
        if len(data) > self.limit and content_type and "json" in content_type:
            # A cut JSON document cannot be parsed by core's key-based masking; mask the
            # visible "key": value pairs so a secret in the first bytes never leaks.
            redactor = recorder.redactor
            text = data.decode("utf-8", errors="ignore")
            text = _JSON_PAIR.sub(
                lambda m: (
                    f'{m.group(1)}{m.group(2)}"[redacted]"'
                    if redactor.is_secret_key(m.group(1)[1:-1])
                    else m.group(0)
                ),
                text,
            )
            data = text.encode("utf-8")
        return data


def _with_header(headers: list[tuple[bytes, bytes]], name: bytes, value: str) -> None:
    """Set ``name`` to ``value``, appending (``, ``) to an existing value."""
    for i, (key, old) in enumerate(headers):
        if key.lower() == name:
            headers[i] = (key, old + b", " + value.encode("latin-1"))
            return
    headers.append((name, value.encode("latin-1")))


def _set_length(headers: list[tuple[bytes, bytes]], length: int) -> list[tuple[bytes, bytes]]:
    out = [(k, v) for k, v in headers if k.lower() != b"content-length"]
    out.append((b"content-length", str(length).encode("latin-1")))
    return out


def _badge_candidate(status: int, headers: list[tuple[bytes, bytes]]) -> bool:
    if not ui.badge_allowed(
        status,
        _header(headers, b"content-type"),
        content_encoding=_header(headers, b"content-encoding"),
        csp=", ".join(
            v.decode("latin-1") for k, v in headers if k.lower() == b"content-security-policy"
        ),
    ):
        return False
    if _header(headers, b"content-disposition") is not None:
        return False  # a file / download, not a page being viewed
    length = _header(headers, b"content-length")
    if length is None:
        return True  # decided on the first body message (a single message only)
    try:
        return 0 < int(length) <= ui.BADGE_MAX_BYTES
    except ValueError:
        return False


class RedlineMiddleware:
    def __init__(
        self,
        app: Any,
        recorder: Recorder | None = None,
        mount: str = "",
        force_ok: Callable[[Any], bool] | None = None,
    ) -> None:
        self.app = app
        self.recorder = recorder
        self.mount = mount
        #: ``force_ok(scope)``: may this request force profiling (``X-Redline-Profile``,
        #: ``?_redline=profile``)? Called only when such a flag is present.
        self.force_ok = force_ok or (lambda scope: bool(getattr(scope.get("app"), "debug", False)))

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        recorder = self.recorder or get_recorder()
        path: str = scope["path"]
        if not recorder.should_record(path):
            await self.app(scope, receive, send)
            return

        settings = recorder.settings
        headers = list(scope.get("headers") or [])
        query = scope.get("query_string", b"")
        client = scope.get("client")
        builder = recorder.start_trace(
            framework="litestar",
            method=scope["method"],
            path=path,
            headers=headers,
            query=query,
            client=client[0] if client else None,
            profile_requested=_wants_profile(headers, query) and self.force_ok(scope),
        )
        limit = settings.max_body_bytes
        req = _Capture(limit) if settings.capture_request_body else None
        res = _Capture(limit)
        capture_res = settings.capture_response_body
        state: dict[str, Any] = {"status": None, "headers": [], "done": False}
        debug = bool(getattr(scope.get("app"), "debug", False))
        badge_on = builder.active and ui.badge_enabled(settings, debug)
        timing_on = ui.server_timing_enabled(settings, debug)
        held: dict[str, Any] = {}  # badge: the held start message and the buffered body

        def finalize(status: int | None = None, error: BaseException | None = None) -> None:
            if state["done"]:
                return
            state["done"] = True
            if req is not None:
                builder.req_body = req.value(recorder, _header(headers, b"content-type"))
            route_path = scope.get("path_template") or (
                scope["path"] if scope.get("route_handler") is not None else None
            )
            status = status or state["status"] or 500
            if error is None and not 400 <= status < 500:
                # a 4xx HTTPException (404, 401...) is a client error, not an error trace
                error = scope.get(EXCEPTION_KEY)
            resp_headers = state["headers"]
            body = None
            if capture_res and _header(resp_headers, b"content-encoding") is None:
                body = res.value(recorder, _header(resp_headers, b"content-type"))
            try:
                recorder.finish_trace(
                    builder,
                    request_object=__import__("starlette.requests", fromlist=["Request"]).Request(
                        scope
                    )
                    if recorder.settings.user_resolver
                    else None,
                    status=status,
                    headers=resp_headers,
                    body=body,
                    size=res.total,
                    route=route_path,
                    view=_endpoint_name(getattr(scope.get("route_handler"), "fn", None)),
                    error=error,
                    path_params=scope.get("path_params") or None,
                )
            except Exception:  # pragma: no cover - recording must never break a request
                import logging

                logging.getLogger("redline").exception("Redline failed to finish a trace")

        async def recv() -> Any:
            message = await receive()
            if req is not None and message["type"] == "http.request":
                chunk = message.get("body", b"")
                if chunk:
                    req.add(chunk)
            return message

        async def forward(message: Any) -> None:
            kind = message["type"]
            if kind == "http.response.body":
                chunk = message.get("body", b"")
                if capture_res:
                    res.add(chunk)
                else:
                    res.total += len(chunk)
            await send(message)
            if kind == "http.response.body" and not message.get("more_body", False):
                finalize()

        async def release(body: bytes) -> None:
            start = held.pop("start")
            held.clear()
            snippet = ui.badge_snippet(
                builder.id,
                f"{scope.get('root_path', '')}{self.mount}",
                ui.badge_summary(builder, state["status"]),
            )
            new_body = ui.inject_badge(body, snippet)
            if new_body is not None:
                body = new_body
                start = {**start, "headers": _set_length(start["headers"], len(body))}
            await forward(start)
            await forward({"type": "http.response.body", "body": body, "more_body": False})

        async def send_wrapper(message: Any) -> None:
            kind = message["type"]
            if kind == "http.response.start":
                state["status"] = message["status"]
                resp_headers = list(message.get("headers") or [])
                if timing_on:
                    timing = builder.server_timing()
                    if timing:
                        _with_header(resp_headers, b"server-timing", timing)
                        message = {**message, "headers": resp_headers}
                state["headers"] = resp_headers
                if badge_on and _badge_candidate(message["status"], resp_headers):
                    held["start"] = message
                    held["body"] = bytearray()
                    held["known"] = _header(resp_headers, b"content-length") is not None
                    return
            elif kind == "http.response.body" and "start" in held:
                chunk = message.get("body", b"")
                if not message.get("more_body", False):
                    await release(bytes(held["body"]) + chunk)
                    return
                if held["known"] and len(held["body"]) + len(chunk) <= ui.BADGE_MAX_BYTES:
                    held["body"] += chunk
                    return
                # streamed without a length: give up and send everything untouched
                start, buffered = held.pop("start"), bytes(held["body"])
                held.clear()
                await forward(start)
                await forward({**message, "body": buffered + chunk})
                return
            await forward(message)

        try:
            async with track_request(builder):  # event-loop lag monitor (blocking spans)
                await self.app(scope, recv, send_wrapper)
        except Exception as exc:
            finalize(error=exc)
            raise
        except BaseException:  # cancellation: the client went away
            finalize(status=state["status"] or 499)
            raise
        else:
            finalize()
