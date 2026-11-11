from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

SECRET_PROMPT = "my password is hunter2-ZZZ and the card is 4111111111111111"


def make_fake(monkeypatch, provider):
    """Install a fake SDK with the same call surface the wrappers patch."""
    calls = []

    class Usage:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    if provider == "anthropic":

        class Messages:
            def create(self, **kwargs):
                calls.append(kwargs)
                if kwargs.get("model") == "boom":
                    raise RuntimeError("upstream failed with " + SECRET_PROMPT)
                return types.SimpleNamespace(
                    usage=Usage(
                        input_tokens=120,
                        output_tokens=30,
                        cache_read_input_tokens=100,
                        cache_creation_input_tokens=5,
                    )
                )

        class AsyncMessages:
            async def create(self, **kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(usage=Usage(input_tokens=7, output_tokens=3))

        mod = types.ModuleType("anthropic.resources.messages")
        mod.Messages, mod.AsyncMessages = Messages, AsyncMessages
        tree = {
            "anthropic": types.ModuleType("anthropic"),
            "anthropic.resources": types.ModuleType("anthropic.resources"),
            "anthropic.resources.messages": mod,
        }
        sync_cls, async_cls = Messages, AsyncMessages
    else:

        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                if kwargs.get("stream"):
                    return iter(())
                return types.SimpleNamespace(
                    usage=Usage(
                        prompt_tokens=50,
                        completion_tokens=9,
                        prompt_tokens_details=Usage(cached_tokens=40),
                    )
                )

        class AsyncCompletions:
            async def create(self, **kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(usage=Usage(prompt_tokens=2, completion_tokens=1))

        mod = types.ModuleType("openai.resources.chat.completions")
        mod.Completions, mod.AsyncCompletions = Completions, AsyncCompletions
        tree = {
            "openai": types.ModuleType("openai"),
            "openai.resources": types.ModuleType("openai.resources"),
            "openai.resources.chat": types.ModuleType("openai.resources.chat"),
            "openai.resources.chat.completions": mod,
        }
        sync_cls, async_cls = Completions, AsyncCompletions
    for name, module in tree.items():
        monkeypatch.setitem(sys.modules, name, module)
    return sync_cls, async_cls, calls


@pytest.fixture(params=["anthropic", "openai"])
def provider(request, monkeypatch):
    sync_cls, async_cls, calls = make_fake(monkeypatch, request.param)
    if request.param == "anthropic":
        from redline_core.contrib import anthropic as mod

        inst, uninst = mod.instrument_anthropic, mod.uninstrument_anthropic
        kwargs = {
            "model": "claude-x",
            "max_tokens": 64,
            "system": "be brief",
            "messages": [{"role": "user", "content": SECRET_PROMPT}],
        }
    else:
        from redline_core.contrib import openai as mod

        inst, uninst = mod.instrument_openai, mod.uninstrument_openai
        kwargs = {"model": "gpt-x", "messages": [{"role": "user", "content": SECRET_PROMPT}]}
    uninst()
    assert inst() and inst()  # idempotent
    yield request.param, sync_cls, async_cls, kwargs, calls
    uninst()


def test_span_has_tokens_and_no_prompt_text(tracer, provider):
    name, sync_cls, _, kwargs, calls = provider
    b = tracer.start()
    sync_cls().create(**kwargs)
    (span,) = tracer.finish(b)
    assert span.kind == "llm_call" and span.callsite is not None
    d = span.data
    assert d["provider"] == name and d["model"] == kwargs["model"] and d["status"] == "ok"
    assert d["input_tokens"] in (120, 50) and d["output_tokens"] in (30, 9)
    assert d["cache_read_tokens"] in (100, 40)
    assert len(d["prompt_hash"]) == 16 and d["prompt_chars"] > len(SECRET_PROMPT)
    blob = json.dumps(span.to_dict(), default=str)
    assert "hunter2" not in blob and "4111" not in blob
    assert calls[0] == kwargs  # the call itself is unchanged


def test_identical_prompts_share_a_hash_and_different_ones_do_not(tracer, provider):
    _, sync_cls, _, kwargs, _ = provider
    b = tracer.start()
    sync_cls().create(**kwargs)
    sync_cls().create(**kwargs)
    sync_cls().create(**{**kwargs, "messages": [{"role": "user", "content": "other"}]})
    a, a2, c = tracer.finish(b)
    assert a.data["prompt_hash"] == a2.data["prompt_hash"] != c.data["prompt_hash"]


def test_async_calls_are_recorded(tracer, provider):
    _, _, async_cls, kwargs, _ = provider
    b = tracer.start()
    asyncio.run(async_cls().create(**kwargs))
    (span,) = tracer.finish(b)
    assert span.kind == "llm_call" and span.data["status"] == "ok"


def test_errors_propagate_and_are_recorded_without_the_message(tracer, provider, monkeypatch):
    name, sync_cls, _, kwargs, _ = provider
    if name == "openai":
        pytest.skip("fake openai has no failing model")
    b = tracer.start()
    with pytest.raises(RuntimeError):
        sync_cls().create(**{**kwargs, "model": "boom"})
    (span,) = tracer.finish(b)
    assert span.data["status"] == "error" and span.data["error_type"] == "RuntimeError"
    assert "hunter2" not in json.dumps(span.to_dict(), default=str)


def test_streaming_is_marked_and_has_no_token_guess(tracer, monkeypatch):
    sync_cls, _, _ = make_fake(monkeypatch, "openai")
    from redline_core.contrib import openai as mod

    mod.uninstrument_openai()
    mod.instrument_openai()
    b = tracer.start()
    sync_cls().create(model="gpt-x", messages=[], stream=True)
    (span,) = tracer.finish(b)
    mod.uninstrument_openai()
    assert span.data["stream"] is True and span.data.get("input_tokens") is None


def test_without_a_trace_nothing_is_recorded_and_call_works(provider):
    _, sync_cls, _, kwargs, calls = provider
    sync_cls().create(**kwargs)
    assert len(calls) == 1


def test_sdk_not_installed_is_a_noop(monkeypatch):
    from redline_core.contrib import anthropic as mod

    for name in [n for n in sys.modules if n.startswith("anthropic")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [m for m in sys.meta_path if "anthropic" not in repr(m)])
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import raises ImportError
    assert mod.instrument_anthropic() is False


def test_uninstrument_restores_original(monkeypatch):
    sync_cls, _, _ = make_fake(monkeypatch, "anthropic")
    from redline_core.contrib import anthropic as mod

    original = sync_cls.create
    mod.uninstrument_anthropic()
    mod.instrument_anthropic()
    assert sync_cls.create is not original
    mod.uninstrument_anthropic()
    assert sync_cls.create is original
