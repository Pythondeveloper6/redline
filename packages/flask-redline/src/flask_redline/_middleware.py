"""The WSGI recording middleware, wrapped around ``app.wsgi_app``.

It owns the *request-level* facts that Flask's hooks cannot see: total time, the final
status and headers, and the response body. The route template, view, template spans and
errors come from Flask's own hooks (``_hooks.py``); they reach the trace through the
contextvar, so both halves write into the same trace.

* the request body is *teed* as the app reads it (the app sees the same bytes, nothing is
  read up front);
* the response is wrapped in an iterator that keeps a size-capped copy of the body – a
  stream (``stream_with_context``, a generator) is never buffered beyond
  ``max_body_bytes`` and is not consumed early;
* the trace finishes when the last chunk has been handed to the server (or the server
  closes the response), so a generator's own work counts towards the latency;
* ``direct_passthrough`` responses (``send_file``, anything served by the server's
  ``wsgi.file_wrapper``) are returned untouched, so ``sendfile`` still works; only the size
  is recorded.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from werkzeug.datastructures import EnvironHeaders
from werkzeug.wsgi import FileWrapper, get_path_info

from redline_core import Recorder
from redline_core.recorder import TraceBuilder
from redline_core.sampling import profile_requested

__all__ = ["ERROR_KEY", "PASSTHROUGH_KEY", "RedlineMiddleware"]

logger = logging.getLogger("redline")

#: ``environ`` keys the Flask hooks use to talk to the middleware.
ERROR_KEY = "redline.error"
PASSTHROUGH_KEY = "redline.passthrough"

_JSON_PAIR = re.compile(r'("(?:[^"\\]|\\.)*")(\s*:\s*)("(?:[^"\\]|\\.)*"|[^,}\]\s]+)')


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


class _TeeInput:
    """``wsgi.input`` that copies what the app reads into a :class:`_Capture`."""

    def __init__(self, stream: Any, capture: _Capture) -> None:
        self._stream = stream
        self._capture = capture

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self._capture.add(data)
        return data

    def readline(self, size: int = -1) -> bytes:
        data = self._stream.readline(size)
        self._capture.add(data)
        return data

    def readlines(self, hint: int = -1) -> list[bytes]:
        lines = self._stream.readlines(hint)
        for line in lines:
            self._capture.add(line)
        return lines

    def readinto(self, buffer: Any) -> int | None:
        # werkzeug's LimitedStream prefers ``readinto`` when the stream has one.
        if hasattr(self._stream, "readinto"):
            n = self._stream.readinto(buffer)
        else:
            data = self._stream.read(len(buffer))
            n = len(data)
            buffer[:n] = data
        if n:
            self._capture.add(bytes(buffer[:n]))
        return n

    def __iter__(self) -> Iterator[bytes]:
        yield from iter(self.readline, b"")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _Body:
    """The response iterable: passes chunks through, copies a capped prefix, then finishes."""

    def __init__(
        self, inner: Iterable[bytes], capture: _Capture | None, finish: Callable[..., None]
    ) -> None:
        self._inner = inner
        self._iter = iter(inner)
        self._capture = capture
        self._finish = finish
        self._total = 0

    def __iter__(self) -> _Body:
        return self

    def __next__(self) -> bytes:
        try:
            chunk = next(self._iter)
        except StopIteration:
            self._finish(size=self._total)
            raise
        except Exception as exc:  # the generator failed after the headers went out
            self._finish(size=self._total, error=exc)
            raise
        self._total += len(chunk)
        if self._capture is not None:
            self._capture.add(chunk)
        return chunk

    def close(self) -> None:
        try:
            close = getattr(self._inner, "close", None)
            if close is not None:
                close()
        finally:
            self._finish(size=self._total)


def _header(headers: Iterable[tuple[str, str]], name: str) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


class RedlineMiddleware:
    """Record one trace per request. Wraps ``app.wsgi_app`` (see ``Redline.init_app``)."""

    def __init__(self, wsgi_app: Any, recorder: Recorder, prefix: str = "/_redline") -> None:
        self.wsgi_app = wsgi_app
        self.recorder = recorder
        self.prefix = prefix

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Any:
        recorder = self.recorder
        script = environ.get("SCRIPT_NAME", "").encode("latin-1").decode("utf-8", "replace")
        path_info = get_path_info(environ)
        path = script + path_info
        prefix = self.prefix
        if (
            path_info == prefix
            or path_info.startswith(prefix + "/")
            or not recorder.should_record(path)
        ):
            return self.wsgi_app(environ, start_response)

        settings = recorder.settings
        headers = dict(EnvironHeaders(environ).items())
        query = environ.get("QUERY_STRING", "")
        builder = recorder.start_trace(
            framework="flask",  # type: ignore[arg-type]
            method=environ.get("REQUEST_METHOD", "GET"),
            path=path,
            headers=headers,
            query=query,
            client=environ.get("REMOTE_ADDR") or None,
            profile_requested=profile_requested(query, headers.get("X-Redline-Profile")),
        )
        limit = settings.max_body_bytes
        req_type = headers.get("Content-Type")
        req: _Capture | None = None
        if (
            settings.capture_request_body
            and environ.get("wsgi.input") is not None
            and not (req_type or "").startswith("multipart/")
        ):
            req = _Capture(limit)
            environ["wsgi.input"] = _TeeInput(environ["wsgi.input"], req)
        res = _Capture(limit) if settings.capture_response_body else None
        state: dict[str, Any] = {"status": None, "headers": [], "done": False, "written": 0}

        def finish(
            status: int | None = None, error: BaseException | None = None, size: int | None = None
        ) -> None:
            if state["done"]:
                return
            state["done"] = True
            try:
                if req is not None and req.total:
                    builder.req_body = req.value(recorder, req_type)
                resp_headers = state["headers"]
                final = status or state["status"] or 500
                error = error or environ.get(ERROR_KEY)
                if error is not None and 400 <= final < 500:
                    error = None  # abort(404) & co.: a client error, not a failure
                body = None
                if res is not None and _header(resp_headers, "content-encoding") is None:
                    body = res.value(recorder, _header(resp_headers, "content-type"))
                if size is None:
                    length = _header(resp_headers, "content-length")
                    size = int(length) if length and length.isdigit() else None
                recorder.finish_trace(
                    builder,
                    status=final,
                    headers=resp_headers,
                    body=body,
                    size=size,
                    error=error,
                )
            except Exception:  # pragma: no cover - recording must never break a request
                logger.exception("Redline failed to finish a trace")

        def start(status: str, response_headers: list[tuple[str, str]], exc_info: Any = None):
            state["status"] = int(status.split(None, 1)[0])
            response_headers = self._finalise_headers(builder, response_headers)
            state["headers"] = response_headers
            write = start_response(status, response_headers, exc_info)

            def write_wrapper(data: bytes) -> Any:
                state["written"] += len(data)
                if res is not None:
                    res.add(data)
                return write(data)

            return write_wrapper

        try:
            iterable = self.wsgi_app(environ, start)
        except Exception as exc:
            finish(status=state["status"] or 500, error=exc)
            raise
        except BaseException:  # the client went away / the worker is shutting down
            finish(status=state["status"] or 499)
            raise

        if environ.get(PASSTHROUGH_KEY) or isinstance(iterable, _file_wrappers(environ)):
            # send_file & co.: hand the server its own file wrapper back, untouched.
            finish(size=None)
            return iterable
        return self._finalise_body(_Body(iterable, res, finish), builder, environ)

    # The one place where response headers and body are finalised: Server-Timing, the
    # floating badge and similar response decoration belong in these two methods.
    def _finalise_headers(
        self, builder: TraceBuilder, headers: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        return headers

    def _finalise_body(self, body: _Body, builder: TraceBuilder, environ: dict[str, Any]) -> Any:
        return body


def _file_wrappers(environ: dict[str, Any]) -> tuple[type, ...]:
    server = environ.get("wsgi.file_wrapper")
    return (FileWrapper, server) if isinstance(server, type) else (FileWrapper,)
