"""Fetching an OpenAPI document over HTTP is bounded and stays on http(s)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from redline_core.load import generate


def _serve(handler: type[BaseHTTPRequestHandler]):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


class _Big(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        data = json.dumps({"paths": {}, "pad": "x" * 5000}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _ToFile(_Big):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "ftp://127.0.0.1:9/openapi.json")
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_oversized_document_is_refused(monkeypatch):
    monkeypatch.setattr(generate, "MAX_OPENAPI_BYTES", 1000)
    server, url = _serve(_Big)
    try:
        with pytest.raises(ValueError, match="too large"):
            generate.load_openapi(url + "/openapi.json")
    finally:
        server.shutdown()
        server.server_close()


def test_document_within_the_limit_loads():
    server, url = _serve(_Big)
    try:
        assert generate.load_openapi(url + "/openapi.json")["paths"] == {}
    finally:
        server.shutdown()
        server.server_close()


def test_redirect_off_http_is_refused():
    server, url = _serve(_ToFile)
    try:
        with pytest.raises(Exception, match="redirect"):
            generate.load_openapi(url + "/openapi.json")
    finally:
        server.shutdown()
        server.server_close()
