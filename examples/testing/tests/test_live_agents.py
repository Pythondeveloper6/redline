"""Happy path of the 0.5 agent features on a real database service.

One FastAPI app per vendor records an N+1 and three LLM calls (a stand-in, no network), then
the real command line reads the store the way an agent would: findings with reasons, the
usage summary, the instrumentation report, a fix brief, the MCP server over stdio, the
opt-in diagnosis preview, and the verify-a-fix answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest
from live_database_helpers import VENDORS, capture_settings, isolated_engine

pytestmark = pytest.mark.skipif(
    os.environ.get("REDLINE_TEST_COMPOSE") != "1",
    reason="Requires explicit disposable Compose integration environment",
)

SECRET = "hunter2-live-prompt-secret"


def cli(*args, stdin=None, env_extra=None, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("REDLINE_AI")}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "redline_core.cli", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=cwd,
    )


def build_app(engine, table, store_path):
    from fastapi import FastAPI
    from sqlalchemy import select

    from fastapi_redline import install
    from redline_core import record_span

    app = FastAPI()

    @app.get("/items")
    def items():  # N+1: one query per id
        with engine.connect() as connection:
            for i in range(1, 9):
                connection.execute(select(table.c.id).where(table.c.id == i)).all()
        return {"ok": True}

    @app.get("/assistant")
    async def assistant():
        prompt = "Summarise this order. " + SECRET
        for _ in range(3):
            start = time.perf_counter()
            await asyncio.sleep(0.01)
            record_span(
                "llm_call",
                "anthropic claude-live",
                start,
                time.perf_counter() - start,
                {
                    "provider": "anthropic",
                    "model": "claude-live",
                    "status": "ok",
                    "stream": False,
                    "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:16],
                    "prompt_chars": len(prompt),
                    "input_tokens": 25,
                    "output_tokens": 7,
                },
            )
        return {"ok": True}

    settings = {**capture_settings(store_path), "n_plus_one_threshold": 5}
    extension = install(app, engine=engine, settings=settings)
    return app, extension


@pytest.mark.parametrize("vendor", VENDORS)
def test_agent_workflow_on_a_real_database(vendor, tmp_path):
    from fastapi.testclient import TestClient

    store_path = tmp_path / "agents.db"
    with isolated_engine(vendor) as (engine, table):
        with engine.begin() as connection:
            connection.execute(table.insert(), [{"id": i} for i in range(1, 10)])
        app, extension = build_app(engine, table, store_path)
        try:
            with TestClient(app, base_url="http://localhost") as client:
                assert client.get("/items").json() == {"ok": True}
                assert client.get("/assistant").json() == {"ok": True}
                issues = client.get("/_redline/api/issues?window=1h&sort=impact").json()["items"]
                kinds = {i["kind"] for i in issues}
                assert {"n_plus_one", "duplicate_llm_call", "serial_llm_calls"} <= kinds
                assert all(i["why"]["rule"] and "impact" in i for i in issues)
                llm = client.get("/_redline/api/llm?window=1h").json()
                assert llm["totals"]["calls"] == 3 and llm["totals"]["input_tokens"] == 75
                assert client.get("/_redline/api/instrumentation").status_code == 200
        finally:
            extension.close()

    store = str(store_path)
    n_plus_one = next(i for i in issues if i["kind"] == "n_plus_one")["id"]

    report = cli("instrumentation", "--store", store, "--json")
    assert report.returncode == 0 and json.loads(report.stdout)["traces_sampled"] >= 2

    brief = cli("fix-prompt", n_plus_one, "--store", store)
    assert brief.returncode == 0 and "n_plus_one" in brief.stdout
    assert "redline check verify" in brief.stdout

    preview = cli("diagnose", n_plus_one, "--store", store)
    assert preview.returncode == 0 and "Nothing has been sent" in preview.stdout
    refused = cli("diagnose", n_plus_one, "--store", store, "--send")
    assert refused.returncode == 2

    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_issues", "arguments": {"window": "1h"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "llm_summary", "arguments": {"window": "1h"}},
        },
    ]
    mcp = cli("mcp", "--store", store, stdin="\n".join(map(json.dumps, messages)) + "\n")
    assert mcp.returncode == 0
    replies = {r["id"]: r for r in map(json.loads, mcp.stdout.splitlines()) if "id" in r}
    names = {t["name"] for t in replies[2]["result"]["tools"]}
    assert {"list_issues", "explain_issue", "verify_fix", "llm_summary"} <= names
    assert "n_plus_one" in json.dumps(replies[3]["result"])
    assert "claude-live" in json.dumps(replies[4]["result"])

    everything = "\n".join([report.stdout, brief.stdout, preview.stdout, mcp.stdout])
    assert "hunter2" not in everything  # prompt text never reaches an agent surface


def test_agent_skill_and_llms_docs_are_available(tmp_path):
    out = cli("agent-skill", "--format", "agents")
    assert out.returncode == 0 and "redline" in out.stdout
    written = cli("agent-skill", "--write", str(tmp_path))
    assert written.returncode == 0
    assert any(tmp_path.rglob("SKILL.md"))
