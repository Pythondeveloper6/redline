"""Security regressions for the agent surfaces (MCP, fix prompts, LLM spans, diagnosis)."""

from __future__ import annotations

import io
import json
import os
import stat
import time

import pytest

from redline_core import cli, diagnose
from redline_core.agent.mcp import serve
from redline_core.agent.tools import call_tool
from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.detect import detect
from redline_core.storage.multiple import TraceReader
from redline_core.storage.sqlite import SQLiteStore

from ..conftest import make_trace
from ..contrib.conftest import tracer  # noqa: F401 (fixture)
from ..contrib.test_llm import SECRET_PROMPT, make_fake


@pytest.fixture
def secret_store(tracer, monkeypatch, tmp_path):
    """A store holding a trace from three identical, secret-bearing LLM calls."""
    sync_cls, _, _ = make_fake(monkeypatch, "anthropic")
    from redline_core.contrib import anthropic as mod

    mod.uninstrument_anthropic()
    assert mod.instrument_anthropic()
    try:
        b = tracer.start()
        for _ in range(3):
            sync_cls().create(
                model="claude-x",
                max_tokens=8,
                messages=[{"role": "user", "content": SECRET_PROMPT}],
            )
        spans = tracer.finish(b)
    finally:
        mod.uninstrument_anthropic()
    path = str(tmp_path / "r.db")
    s = SQLiteStore(path, background=False)
    t = make_trace(spans=spans, started_at=time.time() - 5)
    t.issues = detect(t)
    assert any(i.kind == "duplicate_llm_call" for i in t.issues)
    s.save(t)
    s.close()
    return path, t


def everywhere(path, trace, capsys):
    """Collect the text of every agent-facing output for this store."""
    store = TraceReader(path)
    api = RedlineAPI(store, Settings(), framework="other")
    parts = [json.dumps(api.dispatch("GET", "api/issues", {"window": "1h"})[1], default=str)]
    parts.append(json.dumps(api.dispatch("GET", "api/llm", {"window": "1h"})[1]))
    parts.append(json.dumps(api.dispatch("GET", f"api/traces/{trace.id}", {})[1], default=str))
    items = api.dispatch("GET", "api/issues", {"window": "1h"})[1]["items"]
    for item in items:
        parts.append(json.dumps(call_tool("explain_issue", {"issue_id": item["id"]}, store=store)))
        parts.append(diagnose.preview(diagnose.build_payload(item), "anthropic", "m"))
    parts.append(json.dumps(call_tool("get_trace", {"trace_id": trace.id}, store=store)))
    store.close()
    for item in items:
        assert cli.main(["fix-prompt", item["id"], "--store", path]) == 0
        parts.append(capsys.readouterr().out)
    return "\n".join(parts)


def test_prompt_text_never_reaches_any_agent_surface(secret_store, capsys):
    path, trace = secret_store
    blob = everywhere(path, trace, capsys)
    assert len(blob) > 500 and "duplicate_llm_call" in blob and "claude-x" in blob
    for needle in ("hunter2", "4111111111111111", "password"):
        assert needle not in blob


def test_mcp_survives_hostile_input_and_keeps_serving(secret_store):
    path, _ = secret_store
    big = '{"jsonrpc":"2.0","id":1,"method":"' + "x" * (2 * 1024 * 1024) + '"}'
    lines = [
        big,
        "not json",
        "[]",
        '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"../../etc/passwd"}}',
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_issues",'
        '"arguments":{"limit":"many","window":{"a":1}}}}',
        '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"verify_fix",'
        '"arguments":{"report_path":"/etc/passwd","kind":"n_plus_one"}}}',
        '{"jsonrpc":"2.0","id":5,"method":"ping"}',
    ]
    out = io.StringIO()
    serve(path, io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert replies[-1] == {"jsonrpc": "2.0", "id": 5, "result": {}}
    assert "root:" not in out.getvalue()


def test_audit_log_has_no_argument_values_and_is_private(secret_store, tmp_path):
    path, trace = secret_store
    audit = str(tmp_path / "audit.jsonl")
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_trace", "arguments": {"trace_id": trace.id}},
    }
    serve(path, io.StringIO(json.dumps(msg) + "\n"), io.StringIO(), audit_path=audit)
    text = open(audit).read()
    assert trace.id not in text and "get_trace" in text
    assert stat.S_IMODE(os.stat(audit).st_mode) == 0o600


def test_diagnosis_never_contacts_the_network_by_default(secret_store, monkeypatch, capsys):
    path, trace = secret_store

    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(diagnose, "_send", boom)
    monkeypatch.delenv("REDLINE_AI_DIAGNOSIS", raising=False)
    monkeypatch.delenv("REDLINE_AI_API_KEY", raising=False)
    store = TraceReader(path)
    iid = RedlineAPI(store, Settings(), framework="other").dispatch(
        "GET", "api/issues", {"window": "1h"}
    )[1]["items"][0]["id"]
    store.close()
    assert cli.main(["diagnose", iid, "--store", path]) == 0
    assert cli.main(["diagnose", iid, "--store", path, "--send"]) == 2
