"""The pure-ASGI recording middleware.

Not ``BaseHTTPMiddleware``: that wraps the app in extra tasks, breaks contextvars
propagation for sync endpoints and buffers streaming responses. This one:

* *tees* the request body as the app reads it (the app sees the untouched ``receive``);
* wraps ``send`` to capture status, headers and a size-capped copy of the response body –
  a stream is never buffered beyond ``max_body_bytes``;
* finishes the trace right after the last body chunk is sent, so background tasks do not
  count towards latency;
* resolves the route template (``/orders/{id}``) from ``scope["route"]`` once routing ran.
"""

from __future__ import annotations

import re
from typing import Any

from redline_core import Recorder, get_recorder
from redline_core.contrib.asgi import track_request
from redline_core.sampling import profile_requested

__all__ = ["RedlineMiddleware"]

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


class RedlineMiddleware:
    def __init__(self, app: Any, recorder: Recorder | None = None) -> None:
        self.app = app
        self.recorder = recorder

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
            framework="fastapi",
            method=scope["method"],
            path=path,
            headers=headers,
            query=query,
            client=client[0] if client else None,
            profile_requested=_wants_profile(headers, query),
        )
        root_path = scope.get("root_path", "")
        limit = settings.max_body_bytes
        req = _Capture(limit) if settings.capture_request_body else None
        res = _Capture(limit)
        capture_res = settings.capture_response_body
        state: dict[str, Any] = {"status": None, "headers": [], "done": False}

        def finalize(status: int | None = None, error: BaseException | None = None) -> None:
            if state["done"]:
                return
            state["done"] = True
            if req is not None:
                builder.req_body = req.value(recorder, _header(headers, b"content-type"))
            route = scope.get("route")
            route_path = getattr(route, "path", None)
            if route_path is not None:
                mounted = scope.get("root_path", "")
                if mounted != root_path and mounted.startswith(root_path):
                    route_path = mounted[len(root_path) :] + route_path
            resp_headers = state["headers"]
            body = None
            if capture_res and _header(resp_headers, b"content-encoding") is None:
                body = res.value(recorder, _header(resp_headers, b"content-type"))
            try:
                recorder.finish_trace(
                    builder,
                    status=status or state["status"] or 500,
                    headers=resp_headers,
                    body=body,
                    size=res.total,
                    route=route_path,
                    view=_endpoint_name(scope.get("endpoint")),
                    error=error,
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

        async def send_wrapper(message: Any) -> None:
            kind = message["type"]
            if kind == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = list(message.get("headers") or [])
            elif kind == "http.response.body":
                chunk = message.get("body", b"")
                if capture_res:
                    res.add(chunk)
                else:
                    res.total += len(chunk)
            await send(message)
            if kind == "http.response.body" and not message.get("more_body", False):
                finalize()

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
