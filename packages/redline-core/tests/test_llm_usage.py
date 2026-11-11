from __future__ import annotations

import time

from redline_core.agent.tools import call_tool
from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.llm_usage import summarize

from .conftest import make_trace
from .test_llm_detectors import llm


def seed(store):
    store.save(
        make_trace(
            route="/chat",
            path="/chat",
            started_at=time.time() - 5,
            spans=[
                llm(0, input_tokens=100, output_tokens=20, cache_read_tokens=60),
                llm(200, model="small", input_tokens=10, output_tokens=2),
            ],
        )
    )
    store.save(
        make_trace(
            route="/chat",
            path="/chat",
            started_at=time.time() - 4,
            spans=[
                llm(0, input_tokens=50, output_tokens=5, status="error"),
                llm(200, stream=True, input_tokens=None, output_tokens=None),
            ],
        )
    )
    store.save(make_trace(route="/plain", path="/plain", started_at=time.time() - 3))


def test_summary_groups_by_route_and_model(store):
    seed(store)
    out = summarize(store, window=3600)
    assert out["sampled"] is True and out["traces_scanned"] == 3
    rows = {(r["route"], r["model"]): r for r in out["items"]}
    big = rows[("/chat", "claude-x")]
    assert big["calls"] == 3 and big["errors"] == 1 and big["streamed_calls"] == 1
    assert big["input_tokens"] == 150 and big["output_tokens"] == 25
    assert big["cache_read_tokens"] == 60
    assert rows[("/chat", "small")]["calls"] == 1
    assert ("/plain", "claude-x") not in rows
    assert out["totals"]["calls"] == 4 and out["totals"]["input_tokens"] == 160


def test_streamed_calls_are_not_counted_as_zero_tokens_hidden(store):
    seed(store)
    out = summarize(store, window=3600)
    assert out["note"] and "stream" in out["note"].lower()


def test_empty_and_old_traces(store):
    assert summarize(store, window=3600)["items"] == []
    store.save(make_trace(started_at=time.time() - 9999, spans=[llm(0)]))
    assert summarize(store, window=3600)["items"] == []


def test_api_route_and_validation(store):
    seed(store)
    api = RedlineAPI(store, Settings(), framework="other")
    status, data = api.dispatch("GET", "api/llm", {"window": "1h"})
    assert status == 200 and data["totals"]["calls"] == 4
    assert api.dispatch("GET", "api/llm", {"window": "forever"})[0] == 400
    assert api.dispatch("POST", "api/llm")[0] == 405


def test_agent_tool_uses_real_summary(store):
    seed(store)
    out = call_tool("llm_summary", {"window": "1h"}, store=store)
    assert out["totals"]["calls"] == 4


def test_no_prompt_text_in_output(store):
    seed(store)
    import json

    assert "hunter2" not in json.dumps(summarize(store, window=3600))
