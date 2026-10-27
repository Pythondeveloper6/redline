from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

Handler = Callable[[dict], tuple[int, dict[str, str], bytes]]


@dataclass
class App:
    """A tiny WSGI app: ``routes[path] = handler(environ) -> (status, headers, body)``."""

    routes: dict[str, Handler] = field(default_factory=dict)
    calls: list[tuple[str, str, str]] = field(default_factory=list)  # (method, path, check tag)
    delay: dict[str, float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def json(self, path: str, body: Any, status: int = 200, **headers: str) -> None:
        payload = json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json", **headers}
        self.routes[path] = lambda env: (status, hdrs, payload)

    def text(self, path: str, body: str, status: int = 200, ctype: str = "text/plain") -> None:
        self.routes[path] = lambda env: (status, {"Content-Type": ctype}, body.encode())

    def __call__(self, environ: dict, start_response: Any) -> list[bytes]:
        path = environ.get("PATH_INFO", "/")
        query = environ.get("QUERY_STRING")
        full = f"{path}?{query}" if query else path
        with self.lock:
            self.calls.append(
                (environ["REQUEST_METHOD"], full, environ.get("HTTP_X_REDLINE_CHECK", ""))
            )
        if path in self.delay:
            time.sleep(self.delay[path])
        handler = self.routes.get(full) or self.routes.get(path)
        if handler is None:
            status, headers, body = 404, {"Content-Type": "text/plain"}, b"not found"
        else:
            status, headers, body = handler(environ)
        reason = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(status, "X")
        start_response(f"{status} {reason}", [*headers.items(), ("Content-Length", str(len(body)))])
        return [body] if environ["REQUEST_METHOD"] != "HEAD" else []


@pytest.fixture
def app() -> App:
    return App()


@pytest.fixture
def wsgi(app: App) -> Any:
    httpx = pytest.importorskip("httpx")
    return httpx.WSGITransport(app=app)


class _Quiet(WSGIRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def served(app: App) -> Iterator[str]:
    """``app`` on a real loopback port (for CLI tests); yields the base URL."""
    server = make_server("127.0.0.1", 0, app, handler_class=_Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def dead_url() -> str:
    """A loopback URL nobody listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"
