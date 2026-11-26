"""Flows (chained requests with extracted values) and response assertions."""

from __future__ import annotations

import http.server
import json
import threading
from collections import Counter

import pytest

from redline_core.load import engine
from redline_core.load.flows import (
    ExtractError,
    check_expect,
    jsonpath,
    parse_expect,
    parse_extract,
    run_extract,
)
from redline_core.load.profile import ScenarioError
from redline_core.load.scenario import parse_scenario

ITEMS = [{"id": 11, "name": "a"}, {"id": 12, "name": "b"}, {"id": 13, "name": "c"}]


class Shop:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []  # (path, X-Redline-Run)
        self.statuses: Counter[tuple[str, int]] = Counter()


@pytest.fixture
def shop():
    state = Shop()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            path = self.path
            if path == "/items":
                status, body, hdrs = 200, {"items": ITEMS}, {"X-Next": "tok-9"}
            elif path.startswith("/items/"):
                wanted = path.rsplit("/", 1)[1]
                found = next((i for i in ITEMS if str(i["id"]) == wanted), None)
                status, body, hdrs = (200, found, {}) if found else (404, {"error": "no"}, {})
            elif path == "/broken":
                status, body, hdrs = 200, {"error": "boom"}, {}
            elif path == "/empty":
                status, body, hdrs = 200, {"items": []}, {}
            else:
                status, body, hdrs = 200, {"ok": True}, {}
            raw = json.dumps(body).encode()
            with state.lock:
                state.requests.append((path, self.headers.get("X-Redline-Run") or ""))
                state.statuses[(path.split("?")[0].rsplit("/", 1)[0] or path, status)] += 1
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    host, port = server.server_address[:2]
    yield state, f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def run(url, data, rps=20, duration=3):
    sc = parse_scenario({**data, "base_url": url})
    return engine.run(
        sc, profile={"mode": "constant", "rps": rps, "duration_s": duration}, workers=2
    )


# ------------------------------------------------------------------ helpers
def test_jsonpath():
    doc = {"a": {"b": [{"c": 5}, {"c": 6}]}}
    assert jsonpath(doc, "$.a.b[1].c") == 6
    assert jsonpath(doc, "$.a.b[-1].c") == 6
    assert jsonpath([1, 2], "$[0]") == 1
    assert repr(jsonpath(doc, "$.a.x")) == repr(jsonpath(doc, "$.nope"))  # both "missing"
    with pytest.raises(ValueError):
        jsonpath(doc, "a.b")


def test_extract_from_json_header_and_regex():
    specs = parse_extract(
        {
            "id": "$.items[0].id",
            "tok": {"header": "X-Next"},
            "n": {"regex": r"\"name\": \"(\w)\"", "group": 1},
        },
        "extract",
    )
    got = run_extract(specs, {"X-Next": "tok-9"}, json.dumps({"items": ITEMS}).encode())
    assert got["id"] == "11" and got["tok"] == "tok-9" and got["n"] == "a"
    with pytest.raises(ExtractError):
        run_extract(parse_extract({"x": "$.nope"}, "e"), {}, b"{}")
    with pytest.raises(ExtractError):  # a non-scalar is not a usable value
        run_extract(parse_extract({"x": "$.items"}, "e"), {}, json.dumps({"items": [1]}).encode())


def test_expect_rules():
    body = json.dumps({"items": [1], "status": "ok"}).encode()
    ok = parse_expect(
        [{"json": "$.items"}, {"json": "$.status", "equals": "ok"}, {"contains": "ok"},
         {"min_bytes": 5}, {"header": "Content-Type", "contains": "json"}],
        "expect",
    )  # fmt: skip
    assert check_expect(ok, {"Content-Type": "application/json"}, body) is None
    for rule, headers in (
        ({"json": "$.error"}, {}),
        ({"json": "$.status", "equals": "bad"}, {}),
        ({"not_contains": "ok"}, {}),
        ({"min_bytes": 10_000}, {}),
        ({"header": "X-Missing", "equals": "1"}, {}),
    ):
        assert check_expect(parse_expect(rule, "e"), headers, body) is not None


def test_scenario_validation():
    with pytest.raises(ScenarioError, match="no earlier step extracts"):
        parse_scenario(
            {"name": "f", "base_url": "http://x", "flows": [{"steps": [{"path": "/a/{var:id}"}]}]}
        )
    with pytest.raises(ScenarioError, match="only be used in the steps of a flow"):
        parse_scenario({"name": "f", "base_url": "http://x", "requests": [{"path": "/a/{var:id}"}]})
    with pytest.raises(ScenarioError):
        parse_scenario(
            {"name": "f", "base_url": "http://x", "requests": [{"path": "/a", "expect": [{}]}]}
        )
    sc = parse_scenario(
        {"name": "f", "base_url": "http://x", "flows": [{"steps": [{"path": "/a"}]}]}
    )
    assert sc.requests == () and len(sc.flows) == 1


# ------------------------------------------------------------------ end to end
FLOW = {
    "name": "journey",
    "flows": [
        {
            "name": "browse",
            "steps": [
                {"path": "/items", "extract": {"id": "$.items[1].id"}},
                {"path": "/items/{var:id}", "expect": [{"json": "$.name", "equals": "b"}]},
            ],
        }
    ],
}


def test_a_two_step_flow_never_requests_a_missing_item(shop):
    state, url = shop
    result = run(url, FLOW)
    assert result.status == "done" and result.steps[0].errors == 0
    paths = Counter(p for p, _ in state.requests)
    assert set(paths) == {"/items", "/items/12"}
    assert paths["/items"] == paths["/items/12"]
    assert not any(code == 404 for (_, code) in state.statuses)
    flow = result.summary["flows"]["browse"]
    assert flow["error_rate"] == 0 and flow["count"] == paths["/items"]
    assert [s["name"] for s in flow["steps"]] == ["GET /items", "GET /items/{var:id}"]
    assert all(s["count"] == flow["count"] for s in flow["steps"])
    tags = {h.split(";", 1)[1] for _, h in state.requests}
    assert any("flow=browse;fstep=1" in t for t in tags) and any("fstep=2" in t for t in tags)


def test_a_failed_step_ends_the_flow_and_counts_once(shop):
    state, url = shop
    data = {
        "name": "f",
        "flows": [
            {
                "name": "broken",
                "steps": [
                    {"path": "/items", "extract": {"id": "$.items[0].id"}},
                    {"path": "/items/999"},
                    {"path": "/never"},
                ],
            }
        ],
    }
    result = run(url, data, rps=10, duration=2)
    flow = result.summary["flows"]["broken"]
    assert flow["errors"] == flow["count"] > 0
    assert flow["error_breakdown"] == {"404": flow["count"]}
    assert not any(p == "/never" for p, _ in state.requests)
    assert result.steps[0].error_breakdown == {"404": flow["count"]}


def test_a_missing_extract_is_its_own_error_class(shop):
    _, url = shop
    data = {
        "name": "f",
        "flows": [
            {
                "steps": [
                    {"path": "/empty", "extract": {"id": "$.items[0].id"}},
                    {"path": "/x/{var:id}"},
                ]
            }
        ],
    }
    result = run(url, data, rps=10, duration=2)
    assert set(result.steps[0].error_breakdown) == {"extract"}


def test_a_200_with_an_error_body_is_an_assertion_failure(shop):
    _, url = shop
    data = {
        "name": "a",
        "requests": [
            {"path": "/broken", "expect": [{"json": "$.error", "exists": False}]},
            {"path": "/items", "expect": {"json": "$.items[0].id"}, "weight": 1},
        ],
    }
    result = run(url, data, rps=40, duration=3)
    breakdown = result.steps[0].error_breakdown
    assert set(breakdown) == {"assertion"}
    assert result.steps[0].errors > 0 and result.steps[0].ok > 0


def test_max_ms_turns_a_slow_success_into_a_failure(shop):
    _, url = shop
    data = {"name": "m", "requests": [{"path": "/items", "max_ms": 0.0001}]}
    result = run(url, data, rps=10, duration=2)
    assert set(result.steps[0].error_breakdown) == {"assertion"}


def test_old_scenarios_run_unchanged(shop):
    _, url = shop
    result = run(url, {"name": "plain", "requests": [{"path": "/items"}]}, rps=10, duration=2)
    assert result.steps[0].errors == 0 and "flows" not in result.summary


def test_a_network_location_path_is_refused():
    with pytest.raises(ScenarioError, match="network location"):
        parse_scenario(
            {
                "name": "f",
                "base_url": "http://x",
                "flows": [{"steps": [{"path": "//evil.example/x"}]}],
            }
        )


def test_flows_cannot_come_from_the_ui(tmp_path):
    from redline_core.api import RedlineAPI
    from redline_core.conf import Settings
    from redline_core.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "u.db", background=False)
    api = RedlineAPI(store, Settings(load_from_ui=True), run_launcher=lambda *a: "r1")
    body = {
        "name": "x",
        "requests": [{"path": "/"}],
        "profile": {"mode": "constant", "rps": 1, "duration_s": 1},
        "flows": [{"steps": [{"path": "/a"}]}],
    }
    status, payload = api.dispatch("POST", "api/runs", body=body, base_url="http://127.0.0.1:8000")
    assert status == 400 and "flows" in payload["error"]["message"]
    store.close()


def test_the_cli_prints_each_flow_and_step(shop, capsys, tmp_path):
    from redline_core import cli

    _, url = shop
    path = tmp_path / "flow.json"
    path.write_text(json.dumps({**FLOW, "base_url": url}))
    code = cli.main(["load", str(path), "--no-store", "--rps", "10", "--duration", "2s", "-q"])
    out = capsys.readouterr().out
    assert code == 0
    assert (
        "flow browse: ran" in out and "1. GET /items:" in out and "2. GET /items/{var:id}:" in out
    )
