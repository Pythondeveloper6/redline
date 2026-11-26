"""Sessions: flows with think time and a connection of their own."""

from __future__ import annotations

import http.server
import threading

import pytest

from redline_core.load import engine
from redline_core.load.profile import ScenarioError
from redline_core.load.scenario import parse_scenario


@pytest.fixture
def ports():
    seen: list[int] = []
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            with lock:
                seen.append(self.client_address[1])
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", seen
    server.shutdown()
    server.server_close()


def run(url, flow, rps, duration, **extra):
    sc = parse_scenario(
        {"name": "s", "base_url": url, "flows": [{"name": "visit", **flow}], **extra}
    )
    return engine.run(
        sc, profile={"mode": "constant", "rps": rps, "duration_s": duration}, workers=2
    )


def test_think_time_validation():
    base = {"name": "s", "base_url": "http://x"}
    steps = [{"path": "/a"}, {"path": "/b"}]
    ok = parse_scenario({**base, "flows": [{"steps": steps, "think_s": [1, 3]}]})
    assert ok.flows[0].think_s == (1.0, 3.0)
    assert parse_scenario({**base, "flows": [{"steps": steps, "think_s": "2s"}]}).flows[
        0
    ].think_s == (2.0, 2.0)
    assert ok.to_dict()["flows"][0]["think_s"] == [1.0, 3.0]
    for bad in ([3, 1], [1], "soon", -1, [1, 2, 3], 99999):
        with pytest.raises(ScenarioError):
            parse_scenario({**base, "flows": [{"steps": steps, "think_s": bad}]})
    with pytest.raises(ScenarioError):
        parse_scenario({**base, "flows": [{"steps": steps, "own_connection": "yes"}]})


def test_sessions_with_think_time_hold_about_that_many_in_flight(ports):
    url, _ = ports
    # 20 sessions start per second and each lasts about 3 s of think time: about 60 at once
    result = run(url, {"steps": [{"path": "/a"}, {"path": "/b"}], "think_s": 3}, 20, 6)
    assert result.status == "done" and result.steps[0].errors == 0
    peak = result.summary["sessions"]["max_open"]
    assert 45 <= peak <= 80
    assert result.summary["sessions"]["by_step"][1]["max_open"] == peak
    flow = result.summary["flows"]["visit"]
    assert flow["p50_ms"] >= 2900  # the flow latency includes the think time
    assert flow["steps"][1]["p95_ms"] < 500  # a step's own latency does not


def test_a_flow_without_think_time_is_unchanged_in_shape(ports):
    url, _ = ports
    result = run(url, {"steps": [{"path": "/a"}, {"path": "/b"}]}, 10, 2)
    assert result.summary["flows"]["visit"]["p50_ms"] < 500
    assert result.summary["sessions"]["max_open"] >= 1  # still counted, just short-lived


def test_own_connection_gives_each_session_a_connection(ports):
    url, seen = ports
    run(url, {"steps": [{"path": "/a"}, {"path": "/b"}], "think_s": 0.2}, 10, 3)
    shared = len(set(seen))
    seen.clear()
    run(
        url,
        {"steps": [{"path": "/a"}, {"path": "/b"}], "think_s": 0.2, "own_connection": True},
        10,
        3,
    )
    own = len(set(seen))
    assert own >= 25  # about one connection per session (30 sessions)
    assert shared < own


def test_a_stop_cuts_the_think_time_short(ports):
    import threading
    import time

    url, _ = ports
    stop = threading.Event()
    sc = parse_scenario(
        {
            "name": "s",
            "base_url": url,
            "flows": [{"steps": [{"path": "/a"}, {"path": "/b"}], "think_s": 30}],
        }
    )
    threading.Timer(1.5, stop.set).start()
    t0 = time.monotonic()
    result = engine.run(
        sc, profile={"mode": "constant", "rps": 5, "duration_s": 20}, workers=1, stop_event=stop
    )
    assert result.status == "stopped" and time.monotonic() - t0 < 15
