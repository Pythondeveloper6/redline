from __future__ import annotations

import http.server
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest


@dataclass
class ServerState:
    headers: list[str] = field(default_factory=list)  # X-Redline-Run values
    paths: list[str] = field(default_factory=list)
    stall_on: int | None = None  # stall the n-th request (1-based) ...
    stall_s: float = 1.0  # ... for this long
    lock: threading.Lock = field(default_factory=threading.Lock)
    count: int = 0


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: ServerState

    def _serve(self, body_expected: bool = False) -> None:
        if body_expected:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
        st = self.state
        with st.lock:
            st.count += 1
            n = st.count
            st.headers.append(self.headers.get("X-Redline-Run") or "")
            st.paths.append(self.path)
        if st.stall_on is not None and n == st.stall_on:
            time.sleep(st.stall_s)
        status = 500 if self.path.startswith("/err") else 404 if self.path == "/missing" else 200
        body = b'{"ok": true}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve(body_expected=True)

    def log_message(self, *args: object) -> None:
        pass


def _start(threaded: bool) -> tuple[http.server.HTTPServer, ServerState, str]:
    state = ServerState()
    handler = type("Handler", (_Handler,), {"state": state})
    cls = http.server.ThreadingHTTPServer if threaded else http.server.HTTPServer
    server = cls(("127.0.0.1", 0), handler)
    server.daemon_threads = True  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    host, port = server.server_address[:2]
    return server, state, f"http://{host}:{port}"


@pytest.fixture
def http_server() -> Iterator[tuple[ServerState, str]]:
    """A threaded HTTP/1.1 server: 200 everywhere, 500 under /err, 404 at /missing."""
    server, state, url = _start(threaded=True)
    yield state, url
    server.shutdown()
    server.server_close()


@pytest.fixture
def serial_server() -> Iterator[tuple[ServerState, str]]:
    """A single-threaded server (one request at a time) – for the stall test."""
    server, state, url = _start(threaded=False)
    yield state, url
    server.shutdown()
    server.server_close()
