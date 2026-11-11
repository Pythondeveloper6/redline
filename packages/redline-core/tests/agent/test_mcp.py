from __future__ import annotations

import io
import json
import time

import pytest

from redline_core import cli
from redline_core.agent.mcp import serve
from redline_core.detect import detect
from redline_core.storage.sqlite import SQLiteStore

from ..conftest import make_callsite, make_trace, sql_span


@pytest.fixture
def store_path(tmp_path):
    path = str(tmp_path / "r.db")
    s = SQLiteStore(path, background=False)
    cs = make_callsite()
    t = make_trace(
        spans=[
            sql_span("SELECT * FROM items WHERE order_id = %s", [i], callsite=cs) for i in range(6)
        ],
        started_at=time.time() - 5,
    )
    t.issues = detect(t)
    s.save(t)
    s.close()
    return path


def run(store_path, *messages, audit=None):
    lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    out = io.StringIO()
    serve(store_path, io.StringIO("\n".join(lines) + "\n"), out, audit_path=audit)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def rpc(id, method, params=None):
    msg = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def test_initialize_and_tools_list(store_path):
    init, listing = run(
        store_path,
        rpc(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc(2, "tools/list"),
    )
    assert init["id"] == 1 and init["result"]["serverInfo"]["name"] == "redline"
    assert "tools" in init["result"]["capabilities"]
    names = {t["name"] for t in listing["result"]["tools"]}
    assert {"list_issues", "get_trace", "verify_fix"} <= names
    assert all("inputSchema" in t for t in listing["result"]["tools"])


def test_tools_call_returns_text_content_json(store_path):
    (resp,) = run(
        store_path, rpc(1, "tools/call", {"name": "list_issues", "arguments": {"window": "1h"}})
    )
    result = resp["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["items"][0]["kind"] == "n_plus_one"


def test_tool_errors_are_results_not_crashes(store_path):
    (resp,) = run(store_path, rpc(1, "tools/call", {"name": "list_issues", "arguments": {"x": 1}}))
    assert resp["result"]["isError"] is True


def test_malformed_input_keeps_server_alive(store_path):
    out = run(
        store_path,
        "this is not json",
        "[1, 2]",
        '{"jsonrpc": "2.0", "id": 5, "method": "nope"}',
        '{"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": "bad"}',
        rpc(7, "ping"),
    )
    assert out[0]["error"]["code"] == -32700
    assert out[1]["error"]["code"] == -32600
    assert out[2]["id"] == 5 and out[2]["error"]["code"] == -32601
    assert out[3]["id"] == 6 and out[3]["error"]["code"] == -32602
    assert out[4]["id"] == 7 and out[4]["result"] == {}


def test_oversized_line_is_rejected(store_path):
    big = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + "a" * 1_100_000 + '"}}'
    out = run(store_path, big, rpc(2, "ping"))
    assert out[0]["error"]["code"] == -32600 and out[1]["id"] == 2


def test_notifications_get_no_reply(store_path):
    assert run(store_path, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


def test_audit_file_written(store_path, tmp_path):
    audit = str(tmp_path / "a.jsonl")
    run(store_path, rpc(1, "tools/call", {"name": "list_issues", "arguments": {}}), audit=audit)
    assert json.loads(open(audit).read().splitlines()[0])["tool"] == "list_issues"


def test_cli_mcp_requires_existing_store(tmp_path, capsys):
    assert cli.main(["mcp", "--store", str(tmp_path / "missing.db")]) == 2


def test_cli_mcp_serves_stdin(store_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(rpc(1, "ping")) + "\n"))
    assert cli.main(["mcp", "--store", store_path]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == 1
