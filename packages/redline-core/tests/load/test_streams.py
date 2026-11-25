"""Time to first byte vs last byte, held streams and held websockets."""

from __future__ import annotations

import asyncio
import http.server
import threading
import time

import pytest

from redline_core.load import engine
from redline_core.load.profile import ScenarioError
from redline_core.load.scenario import parse_scenario

websockets = pytest.importorskip("websockets")


class SSE(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/slow-body":  # headers at once, body 0.4 s later
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.flush()
            time.sleep(0.4)
            self.wfile.write(b"ok")
            return
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for i in range(100):
                    event = f"data: {i}\n\n".encode()
                    self.wfile.write(f"{len(event):x}\r\n".encode() + event + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def sse():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SSE)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def ws_server():
    """An echo websocket server on its own event loop thread."""
    started = threading.Event()
    box: dict = {}

    async def echo(ws):
        async for message in ws:
            await ws.send(message)

    async def main():
        async with websockets.serve(echo, "127.0.0.1", 0) as server:
            box["port"] = next(iter(server.sockets)).getsockname()[1]
            box["stop"] = asyncio.Event()
            box["loop"] = asyncio.get_running_loop()
            started.set()
            await box["stop"].wait()

    thread = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    thread.start()
    assert started.wait(5)
    yield f"http://127.0.0.1:{box['port']}"
    box["loop"].call_soon_threadsafe(box["stop"].set)
    thread.join(5)


def run(url, requests, profile, **kw):
    sc = parse_scenario({"name": "t", "base_url": url, "requests": requests, **kw})
    return engine.run(sc, profile=profile, workers=2)


# ------------------------------------------------------------------ validation
def test_request_type_validation():
    base = {"name": "t", "base_url": "http://x"}
    with pytest.raises(ScenarioError, match="hold_s"):
        parse_scenario({**base, "requests": [{"path": "/e", "type": "stream"}]})
    with pytest.raises(ScenarioError, match="messages or hold_s"):
        parse_scenario({**base, "requests": [{"path": "/w", "type": "websocket"}]})
    with pytest.raises(ScenarioError, match="only a websocket"):
        parse_scenario(
            {**base, "requests": [{"path": "/e", "type": "stream", "hold_s": 1, "messages": ["x"]}]}
        )
    with pytest.raises(ScenarioError, match="type"):
        parse_scenario({**base, "requests": [{"path": "/e", "type": "carrier-pigeon"}]})
    with pytest.raises(ScenarioError, match="body"):
        parse_scenario(
            {**base, "requests": [{"path": "/e", "type": "stream", "hold_s": 1, "body": "x"}]}
        )


# ------------------------------------------------------------------ first byte vs last byte
def test_first_byte_and_last_byte_are_measured_separately(sse):
    result = run(sse, [{"path": "/slow-body"}], {"mode": "constant", "rps": 5, "duration_s": 2})
    step = result.steps[0]
    assert step.ttfb_hist.percentile(95) < 200  # headers arrive at once
    assert step.hist.percentile(50) > 350  # the last byte comes 0.4 s later
    assert result.summary["ttfb_p95_ms"] < 200 < result.summary["p95_ms"]


def test_a_streamed_endpoint_reports_ttfb_and_ttlb_separately(sse):
    result = run(
        sse,
        [{"path": "/events", "type": "stream", "hold_s": 1.5}],
        {"mode": "constant", "rps": 4, "duration_s": 2},
        connections=64,
    )
    step = result.steps[0]
    assert result.status == "done" and step.errors == 0
    assert step.ttfb_hist.percentile(95) < 300  # first event arrives at once
    assert step.hist.percentile(95) < 300  # the latency of a held stream is its first byte
    streams = result.summary["streams"]
    assert streams["count"] >= 6 and streams["events"] >= streams["count"] * 10
    assert streams["by_step"][1]["ttlb_p50_ms"] >= 1400  # last byte: the end of the hold
    assert streams["max_open"] >= 2


# ------------------------------------------------------------------ websockets
def test_two_hundred_held_websockets_show_as_concurrent_connections(ws_server):
    result = run(
        ws_server,
        [{"path": "/ws", "type": "websocket", "messages": ["ping"], "hold_s": 3}],
        {"mode": "constant", "rps": 100, "duration_s": 2},
    )
    assert result.status == "done" and result.steps[0].errors == 0
    ws = result.summary["websockets"]
    assert ws["count"] == 200 and ws["messages"] == 200
    assert ws["max_open"] >= 150  # about 200 connections were open at once
    row = ws["by_step"][1]
    assert row["connect_p95_ms"] > 0 and row["latency_p95_ms"] > 0


def test_a_websocket_that_cannot_connect_is_an_error(sse):
    result = run(
        sse,
        [{"path": "/nope", "type": "websocket", "messages": ["x"]}],
        {"mode": "constant", "rps": 5, "duration_s": 1},
    )
    assert result.steps[0].errors == result.steps[0].sent > 0


def test_the_cli_prints_first_byte_and_streams(sse, tmp_path, capsys):
    import json

    from redline_core import cli

    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"name": "s", "base_url": sse,
                    "requests": [{"path": "/events", "type": "stream", "hold_s": 1}]})
    )  # fmt: skip
    code = cli.main(["load", str(path), "--no-store", "--rps", "3", "--duration", "2s", "-q"])
    out = capsys.readouterr().out
    assert (
        code == 0 and "first byte: p50" in out and "streams: " in out and "peak concurrent" in out
    )


# ------------------------------------------------------------------ browser pages
def test_requests_of_one_page_load_are_summed(tmp_path):
    from redline_core.api import RedlineAPI
    from redline_core.conf import Settings
    from redline_core.recorder import Recorder
    from redline_core.runs import Run
    from redline_core.storage import SQLiteStore

    store = SQLiteStore(str(tmp_path / "p.db"), background=False)
    store.save_run(Run(id="br", name="browser", status="running"))
    rec = Recorder(Settings(run_metrics=False), store)
    for path, page in (("/", "home-1"), ("/api/items", "home-1"), ("/api/user", "home-1"),
                       ("/cart", "cart-2"), ("/static/x", None)):  # fmt: skip
        tag = f"br;step=1;page={page}" if page else "br;step=1"
        with rec.recording(method="GET", path=path, headers={"X-Redline-Run": tag}):
            pass
    store.flush()
    status, payload = RedlineAPI(store, Settings()).dispatch("GET", "api/runs/br/pages")
    assert status == 200
    by_page = {p["page"]: p for p in payload["items"]}
    assert set(by_page) == {"home-1", "cart-2"}
    assert by_page["home-1"]["requests"] == 3 and by_page["cart-2"]["requests"] == 1
    assert RedlineAPI(store, Settings()).dispatch("GET", "api/runs/none/pages")[0] == 404
    store.close()
