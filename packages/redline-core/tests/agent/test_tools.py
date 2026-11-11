from __future__ import annotations

import json
import time

import pytest

from redline_core.agent.audit import AuditLog
from redline_core.agent.tools import TOOLS, call_tool
from redline_core.detect import detect
from redline_core.storage.multiple import TraceReader
from redline_core.storage.sqlite import SQLiteStore

from ..conftest import make_callsite, make_trace, sql_span


@pytest.fixture
def seeded_path(tmp_path):
    path = str(tmp_path / "r.db")
    s = SQLiteStore(path, background=False)
    cs = make_callsite()
    spans = [
        sql_span("SELECT * FROM items WHERE order_id = %s", [i], callsite=cs) for i in range(6)
    ]
    t = make_trace(spans=spans, started_at=time.time() - 5)
    t.issues = detect(t)
    s.save(t)
    s.close()
    return path, t


@pytest.fixture
def reader(seeded_path):
    r = TraceReader(seeded_path[0])
    yield r
    r.close()


def test_tool_registry_is_read_only_and_described():
    assert {"list_issues", "explain_issue", "get_trace", "instrumentation_report"} <= set(TOOLS)
    for tool in TOOLS.values():
        assert tool.description and tool.schema["type"] == "object"
    assert not {"delete_trace", "clear", "run_check", "explain_sql"} & set(TOOLS)


def test_list_issues_returns_ranked_items_with_why(reader):
    out = call_tool("list_issues", {"window": "1h"}, store=reader)
    assert out["items"] and out["items"][0]["kind"] == "n_plus_one"
    assert "why" in out["items"][0] and "impact" in out["items"][0]


def test_list_issues_limit_and_kind(reader):
    assert call_tool("list_issues", {"kind": "slow_query"}, store=reader)["items"] == []
    assert len(call_tool("list_issues", {"limit": 1}, store=reader)["items"]) == 1


def test_explain_issue_by_id_and_unknown(reader):
    iid = call_tool("list_issues", {}, store=reader)["items"][0]["id"]
    out = call_tool("explain_issue", {"issue_id": iid}, store=reader)
    assert out["id"] == iid and out["why"]["rule"]
    assert "error" in call_tool("explain_issue", {"issue_id": "nope"}, store=reader)


def test_get_trace_is_bounded_and_has_no_bodies(seeded_path, reader):
    _, t = seeded_path
    out = call_tool("get_trace", {"trace_id": t.id}, store=reader)
    assert out["id"] == t.id and len(out["spans"]) <= 50
    assert "request" not in out and "response" not in out and "logs" not in out
    assert "error" in call_tool("get_trace", {"trace_id": "missing"}, store=reader)


def test_bad_arguments_never_raise(reader):
    assert "error" in call_tool("list_issues", {"window": "forever"}, store=reader)
    assert "error" in call_tool("list_issues", {"limit": "x"}, store=reader)
    assert "error" in call_tool("list_issues", {"limit": 10_000}, store=reader)
    assert "error" in call_tool("list_issues", {"bogus": 1}, store=reader)
    assert "error" in call_tool("nope", {}, store=reader)
    assert "error" in call_tool("get_trace", {}, store=reader)


def test_empty_store_gives_empty_results(tmp_path):
    path = str(tmp_path / "e.db")
    SQLiteStore(path, background=False).close()
    r = TraceReader(path)
    assert call_tool("list_issues", {}, store=r)["items"] == []
    assert call_tool("instrumentation_report", {}, store=r)["traces_sampled"] == 0
    r.close()


def test_verify_fix_tool_reads_a_report_file(tmp_path, reader):
    from redline_core.check.report import FORMAT, VERSION

    p = tmp_path / "rep.json"
    p.write_text(json.dumps({"format": FORMAT, "version": VERSION, "id": "x", "requests": []}))
    out = call_tool("verify_fix", {"report_path": str(p), "kind": "n_plus_one"}, store=reader)
    assert out["finding"] == "inconclusive"
    assert "error" in call_tool(
        "verify_fix", {"report_path": str(tmp_path / "no.json"), "kind": "n_plus_one"}, store=reader
    )


def test_output_strings_are_truncated(reader):
    out = call_tool("list_issues", {}, store=reader)
    assert all(len(json.dumps(i)) < 20_000 for i in out["items"])


def test_audit_log_records_calls_without_argument_values(tmp_path, reader):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    call_tool("list_issues", {"window": "1h"}, store=reader, audit=log)
    call_tool("nope", {"secret": "s3cr3t"}, store=reader, audit=log)
    lines = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [x["tool"] for x in lines] == ["list_issues", "nope"]
    assert lines[0]["ok"] is True and lines[1]["ok"] is False
    assert "s3cr3t" not in (tmp_path / "audit.jsonl").read_text()
    assert oct((tmp_path / "audit.jsonl").stat().st_mode & 0o777) == "0o600"
