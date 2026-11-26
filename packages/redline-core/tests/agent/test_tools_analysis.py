from __future__ import annotations

import json
import time

import pytest

from redline_core.agent.base import clip, validate
from redline_core.agent.tools import TOOLS, call_tool
from redline_core.check.report import FORMAT, VERSION
from redline_core.detect import detect
from redline_core.storage.multiple import TraceReader
from redline_core.storage.sqlite import SQLiteStore

from ..conftest import make_callsite, make_trace, sql_span

PLAN = (
    '<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan" Version="1.5">'
    '<BatchSequence><Batch><Statements><StmtSimple StatementText="SELECT 1">'
    '<QueryPlan><RelOp NodeId="0" PhysicalOp="Table Scan" LogicalOp="Table Scan" '
    'EstimateRows="50000" EstimatedTotalSubtreeCost="12.5">'
    '<TableScan><Object Schema="[dbo]" Table="[orders]"/></TableScan></RelOp>'
    "</QueryPlan></StmtSimple></Statements></Batch></BatchSequence></ShowPlanXML>"
)


@pytest.fixture
def seeded(tmp_path):
    path = str(tmp_path / "r.db")
    s = SQLiteStore(path, background=False)
    cs = make_callsite()
    traces = []
    for _ in range(2):
        t = make_trace(
            spans=[
                sql_span("SELECT * FROM items WHERE order_id = %s", [i], callsite=cs)
                for i in range(6)
            ],
            started_at=time.time() - 5,
        )
        t.issues = detect(t)
        s.save(t)
        traces.append(t)
    s.close()
    return path, traces


@pytest.fixture
def reader(seeded):
    r = TraceReader(seeded[0])
    yield r
    r.close()


def test_every_tool_has_a_strict_schema_and_description():
    for tool in TOOLS.values():
        assert tool.description and tool.schema["type"] == "object"
        assert tool.schema["additionalProperties"] is False, tool.name
        assert set(tool.schema["required"]) <= set(tool.schema["properties"]), tool.name
    read_only = {t.name for t in TOOLS.values() if t.gate is None}
    assert not read_only & {"start_load_run", "stop_load_run", "delete_load_run"}


def test_schema_validation_rules():
    tool = TOOLS["list_traces"]
    with pytest.raises(ValueError, match="whole number"):
        validate(tool, {"limit": 1.5})
    with pytest.raises(ValueError, match="at most"):
        validate(tool, {"limit": 1000})
    with pytest.raises(ValueError, match="boolean"):
        validate(tool, {"has_issues": "yes"})
    with pytest.raises(ValueError, match="whole number"):
        validate(tool, {"min_queries": True})
    with pytest.raises(ValueError, match="one of"):
        validate(TOOLS["lint_sql"], {"sql": "x", "dialect": "oracle"})
    with pytest.raises(ValueError, match="longer than"):
        validate(TOOLS["lint_sql"], {"sql": "x" * 100_001})
    with pytest.raises(ValueError, match="required"):
        validate(TOOLS["lint_sql"], {"sql": ""})


def test_clip_masks_secrets_and_bounds_output():
    out = clip(
        {
            "password": "hunter2",
            "token_count": 5,
            "headers": {"Authorization": "Bearer abcdefghijkl"},
            "url": "postgresql://app:pw@db.example.test/x?password=abc",
            "text": "connect failed: token=abc123 and Bearer abcdefghijklmn",
            "long": "x" * 5000,
            "many": list(range(500)),
        }
    )
    assert out["password"] == "[redacted]" and out["token_count"] == 5
    assert out["headers"] == {"Authorization": "[redacted]"}
    assert "pw@" not in out["url"] and "abc" not in out["url"].split("?")[1]
    assert "abc123" not in out["text"] and "abcdefghijklmn" not in out["text"]
    assert len(out["long"]) < 2100 and len(out["many"]) == 100


def test_oversized_results_become_an_error(reader, monkeypatch):
    from redline_core.agent import tools

    monkeypatch.setattr(tools, "MAX_OUTPUT", 100)
    assert "too large" in call_tool("list_traces", {}, store=reader)["error"]


def test_list_traces_filters(reader):
    assert len(call_tool("list_traces", {}, store=reader)["items"]) == 2
    assert len(call_tool("list_traces", {"limit": 1}, store=reader)["items"]) == 1
    assert call_tool("list_traces", {"has_issues": True, "min_queries": 6}, store=reader)["items"]
    assert call_tool("list_traces", {"min_queries": 100}, store=reader)["items"] == []
    assert "error" in call_tool("list_traces", {"status": "9zz"}, store=reader)


def test_endpoints_sql_and_index_advice(reader):
    eps = call_tool("list_endpoints", {"window": "1h"}, store=reader)
    assert eps["items"][0]["count"] == 2
    assert "error" in call_tool("list_endpoints", {"group_by": "bogus"}, store=reader)
    found = call_tool("search_sql", {"q": "items"}, store=reader)
    assert found["items"][0]["count"] == 12
    advice = call_tool("index_advice", {}, store=reader)
    assert advice["candidates"][0]["table"] == "items"


def test_compare_traces(seeded, reader):
    _, (a, b) = seeded
    out = call_tool("compare_traces", {"a": a.id, "b": b.id}, store=reader)
    assert out["a"]["id"] == a.id and out["b"]["id"] == b.id
    assert "error" in call_tool("compare_traces", {"a": a.id, "b": "missing"}, store=reader)
    assert "error" in call_tool("compare_traces", {"a": a.id}, store=reader)


def test_summaries_over_an_empty_window(reader):
    assert call_tool("cache_summary", {}, store=reader)["items"] == []
    assert call_tool("external_summary", {}, store=reader)["items"] == []
    assert call_tool("list_releases", {}, store=reader)["items"] == []
    assert call_tool("job_summary", {}, store=reader)["total"] == 0
    assert call_tool("history", {"window": "7d"}, store=reader)["series"]
    assert "error" in call_tool("history", {"bucket": "5m"}, store=reader)
    assert "error" in call_tool("compare_periods", {"a": "1h", "b": "2h"}, store=reader)


def test_database_capabilities_without_a_connection_says_so(reader):
    out = call_tool("database_capabilities", {"db": "main"}, store=reader)
    assert out["supported"] is False and out["db"] == "main"


def test_analyze_sql_plan_pasted_xml(reader):
    out = call_tool("analyze_sql_plan", {"plan": PLAN, "sql": "SELECT 1"}, store=reader)
    assert out["uploaded"] is True and "Table Scan" in out["text"]
    assert "plan" not in out and "<ShowPlanXML" not in json.dumps(out)
    assert "error" in call_tool("analyze_sql_plan", {"plan": "<nope/>"}, store=reader)
    assert "error" in call_tool("analyze_sql_plan", {}, store=reader)
    assert "error" in call_tool("analyze_sql_plan", {"plan": PLAN, "vendor": "x"}, store=reader)
    assert "error" in call_tool("analyze_sql_plan", {"plan": "x" * 800_001}, store=reader)
    xxe = "<!DOCTYPE x [<!ENTITY a 'b'>]><x/>"
    assert "error" in call_tool("analyze_sql_plan", {"plan": xxe}, store=reader)


def test_sql_lint_and_scans(reader, tmp_path, monkeypatch):
    out = call_tool(
        "lint_sql", {"sql": "INSERT INTO a SELECT * FROM b", "dialect": "mssql"}, store=reader
    )
    assert out["findings"][0]["kind"] == "select-star-in-insert-select"
    assert call_tool("lint_sql", {"sql": "SELECT 1"}, store=reader)["findings"] == []
    src = "def f(cur):\n    cur.execute('INSERT INTO a SELECT * FROM b')\n"
    scanned = call_tool("scan_sql_source", {"source": src}, store=reader)
    assert scanned["statements"] == 1 and scanned["findings"][0]["line"] == 2
    project = tmp_path / "proj"
    project.mkdir()
    (project / "m.py").write_text(src)
    monkeypatch.chdir(tmp_path)
    found = call_tool("scan_sql_project", {"path": "proj"}, store=reader)
    assert found["files"] == 1 and found["findings"][0]["file"] == "proj/m.py"
    assert "error" in call_tool("scan_sql_project", {"path": ".."}, store=reader)
    assert "error" in call_tool("scan_sql_project", {"path": "proj/m.py"}, store=reader)
    assert "error" in call_tool("scan_sql_project", {"path": "missing"}, store=reader)


def test_static_analysis_tools(reader, tmp_path, monkeypatch):
    out = call_tool("analyze_source", {"source": "x = 1\n"}, store=reader)
    assert out["findings"] == []
    assert "error" in call_tool("analyze_source", {"source": "x" * 200_001}, store=reader)
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "m.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    assert call_tool("analyze_project", {"path": "proj"}, store=reader)["files_analyzed"] == 1
    assert "error" in call_tool("analyze_project", {"path": "/"}, store=reader)


def test_doctor_reports_checks(reader, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = call_tool("doctor", {}, store=reader)
    assert out["checks"] and {c["status"] for c in out["checks"]} <= {"ok", "warn", "fail"}
    assert isinstance(out["ok"], bool) and "preset" in out
    with_production = call_tool("doctor", {"production": True}, store=reader)
    assert len(with_production["checks"]) >= len(out["checks"])
    assert "error" in call_tool("doctor", {"strict": "yes"}, store=reader)


REPORT = {
    "format": FORMAT,
    "version": VERSION,
    "id": "rep1",
    "summary": {"requests": 1},
    "requests": [
        {
            "name": "orders",
            "method": "GET",
            "path": "/orders",
            "perf": {"verdict": "slower", "change_pct": 40.0},
            "response": {"verdict": "equal"},
            "server": {
                "new_issues": [
                    {"kind": "n_plus_one", "title": "N+1 on items", "fingerprint": "fp1"}
                ]
            },
        }
    ],
}


def test_check_report_and_findings(reader, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # report files are read from the working directory only
    path = tmp_path / "rep.json"
    path.write_text(json.dumps(REPORT))
    summary = call_tool("check_report", {"report_path": "rep.json"}, store=reader)
    assert summary["id"] == "rep1" and summary["exit_code"] == 1
    assert summary["requests"][0]["failing"] == ["slower"]
    found = call_tool("check_findings", {"report_path": "rep.json"}, store=reader)
    assert found["findings_total"] == 1 and "n_plus_one" in found["findings"][0]["prompt"]
    for tool in ("check_report", "check_findings"):
        assert "error" in call_tool(tool, {"report_path": "no.json"}, store=reader)
        assert "error" in call_tool(tool, {"report_path": "x.txt"}, store=reader)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert "error" in call_tool("check_report", {"report_path": "bad.json"}, store=reader)
    bad.write_text(json.dumps({"format": "other"}))
    assert "error" in call_tool("check_findings", {"report_path": "bad.json"}, store=reader)


def test_fix_prompt_and_diagnose_preview_never_send(reader):
    iid = call_tool("list_issues", {}, store=reader)["items"][0]["id"]
    brief = call_tool("fix_prompt", {"issue_id": iid}, store=reader)
    assert brief["issue_id"] == iid and "redline check verify" in brief["prompt"]
    preview = call_tool("diagnose_preview", {"issue_id": iid}, store=reader)
    assert preview["sent"] is False and "Nothing has been sent" in preview["preview"]
    for tool in ("fix_prompt", "diagnose_preview"):
        assert "error" in call_tool(tool, {"issue_id": "nope"}, store=reader)
        assert "error" in call_tool(tool, {}, store=reader)


def test_reader_exposes_run_reads_without_writing(seeded):
    path, _ = seeded
    r = TraceReader(path)
    assert r.list_runs() == [] and r.get_run("x") is None
    assert r.run_metric_samples("x") == []
    r.close()
