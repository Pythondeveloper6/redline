from __future__ import annotations

import pytest

from redline_core.conf import Settings
from redline_core.detect import detect
from redline_core.model import Span, new_id

from .conftest import make_callsite, make_trace


def llm(start, dur=100.0, *, h="aaaa", model="claude-x", provider="anthropic", chars=100, **extra):
    data = {
        "provider": provider,
        "model": model,
        "status": "ok",
        "stream": False,
        "prompt_hash": h,
        "prompt_chars": chars,
        "input_tokens": 10,
        "output_tokens": 5,
    }
    data.update(extra)
    return Span(
        id=new_id(),
        parent_id=None,
        kind="llm_call",
        name=f"{provider} {model}",
        start_ms=start,
        duration_ms=dur,
        callsite=make_callsite(),
        data=data,
    )


def tx(start, dur):
    return Span(
        id=new_id(),
        parent_id=None,
        kind="transaction",
        name="tx",
        start_ms=start,
        duration_ms=dur,
        callsite=make_callsite(),
        data={"db_alias": "default", "queries": 1},
    )


def kinds(trace, **settings):
    return {i.kind for i in detect(trace, Settings(**settings))}


def test_duplicate_llm_call_needs_same_prompt_and_model():
    same = make_trace(spans=[llm(0), llm(200), llm(400)])
    assert "duplicate_llm_call" in kinds(same)
    different = make_trace(spans=[llm(0, h="a"), llm(200, h="b"), llm(400, h="c")])
    assert "duplicate_llm_call" not in kinds(different)
    other_model = make_trace(spans=[llm(0), llm(200, model="other")])
    assert "duplicate_llm_call" not in kinds(other_model)


def test_duplicate_threshold_and_disable():
    t = make_trace(spans=[llm(0), llm(200)])
    assert "duplicate_llm_call" in kinds(t)
    assert "duplicate_llm_call" not in kinds(t, llm_duplicate_threshold=3)
    assert "duplicate_llm_call" not in kinds(t, llm_duplicate_threshold=0)


def test_duplicate_evidence_has_count_and_no_prompt_text():
    (issue,) = [
        i
        for i in detect(make_trace(spans=[llm(0), llm(200)]), Settings())
        if i.kind == "duplicate_llm_call"
    ]
    assert issue.count == 2 and issue.extra["calls"] == 2 and issue.extra["threshold"] == 2
    assert "prompt_hash" not in issue.detail


def test_serial_calls_flagged_when_back_to_back_with_different_prompts():
    t = make_trace(spans=[llm(0, h="a"), llm(110, h="b"), llm(220, h="c")])
    assert "serial_llm_calls" in kinds(t)


def test_overlapping_calls_are_not_serial():
    t = make_trace(spans=[llm(0, h="a"), llm(10, h="b"), llm(20, h="c")])
    assert "serial_llm_calls" not in kinds(t)


def test_fewer_than_minimum_is_not_serial():
    t = make_trace(spans=[llm(0, h="a"), llm(110, h="b")])
    assert "serial_llm_calls" not in kinds(t)


def test_serial_issue_states_the_dependency_caveat():
    t = make_trace(spans=[llm(0, h="a"), llm(110, h="b"), llm(220, h="c")])
    (issue,) = [i for i in detect(t, Settings()) if i.kind == "serial_llm_calls"]
    assert "depend" in issue.detail.lower()


def test_llm_call_inside_transaction():
    inside = make_trace(spans=[tx(0, 1000), llm(100)])
    assert "llm_in_transaction" in kinds(inside)
    outside = make_trace(spans=[tx(0, 50), llm(100)])
    assert "llm_in_transaction" not in kinds(outside)


def test_call_loop_threshold():
    many = make_trace(spans=[llm(i * 200, h=str(i)) for i in range(8)])
    assert "llm_call_loop" in kinds(many)
    assert "llm_call_loop" not in kinds(many, llm_loop_calls=9)
    assert "llm_call_loop" not in kinds(many, llm_loop_calls=0)


def test_uncached_large_prompt_only_for_anthropic_nonstreaming_without_cache():
    big = make_trace(spans=[llm(0, chars=9000)])
    assert "uncached_large_prompt" in kinds(big)
    assert "uncached_large_prompt" not in kinds(make_trace(spans=[llm(0, chars=100)]))
    cached = make_trace(spans=[llm(0, chars=9000, cache_read_tokens=500)])
    assert "uncached_large_prompt" not in kinds(cached)
    written = make_trace(spans=[llm(0, chars=9000, cache_creation_tokens=500)])
    assert "uncached_large_prompt" not in kinds(written)
    streamed = make_trace(spans=[llm(0, chars=9000, stream=True)])
    assert "uncached_large_prompt" not in kinds(streamed)
    openai = make_trace(spans=[llm(0, chars=9000, provider="openai")])
    assert "uncached_large_prompt" not in kinds(openai)


def test_no_llm_spans_no_findings():
    assert not {k for k in kinds(make_trace()) if "llm" in k or k == "uncached_large_prompt"}


def test_issue_kinds_registered_and_explained():
    from redline_core.finding_explain import explain_issue
    from redline_core.model import ISSUE_KINDS

    new = {
        "duplicate_llm_call",
        "serial_llm_calls",
        "llm_in_transaction",
        "llm_call_loop",
        "uncached_large_prompt",
    }
    assert new <= set(ISSUE_KINDS)
    for kind in new:
        why = explain_issue(kind, {"threshold": 3}, 3)
        assert "matched the recorded spans" not in why["rule"], kind  # a specific rule exists


@pytest.mark.parametrize(
    "name",
    ["llm_duplicate_threshold", "llm_serial_min", "llm_loop_calls", "llm_prompt_cache_chars"],
)
def test_settings_reject_bad_values(name):
    from redline_core.conf import SettingsError

    with pytest.raises(SettingsError):
        Settings.from_mapping({name: -1})
    with pytest.raises(SettingsError):
        Settings.from_mapping({name: True})
