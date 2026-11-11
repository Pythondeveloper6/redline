"""A stand-in "assistant" so the LLM-call findings can be demoed without an API key or network.

A real app calls the Anthropic or OpenAI SDK; after ``instrument_anthropic()`` /
``instrument_openai()`` each call becomes an ``llm_call`` span like the ones recorded here.
This module records spans of the same shape directly, with the same privacy rule: only the
model, token counts and a hash of the prompt are stored, never its text.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

from fastapi import APIRouter

from redline_core import record_span

router = APIRouter()


async def fake_llm(model: str, prompt: str, *, out_tokens: int = 40) -> str:
    """Pretend to call a model: wait a little and record the span an SDK wrapper would."""
    start = time.perf_counter()
    await asyncio.sleep(0.03)
    record_span(
        "llm_call",
        f"anthropic {model}",
        start,
        time.perf_counter() - start,
        {
            "provider": "anthropic",
            "model": model,
            "status": "ok",
            "stream": False,
            "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:16],
            "prompt_chars": len(prompt),
            "input_tokens": len(prompt) // 4,
            "output_tokens": out_tokens,
        },
    )
    return "ok"


@router.get("/assistant")
async def assistant() -> dict:
    """Three findings in one request: duplicate calls, serial calls, an uncached big prompt."""
    policy = "You are a support assistant. " * 400  # a large, stable prefix, sent uncached
    for _ in range(3):  # duplicate_llm_call: the identical question three times
        await fake_llm("claude-demo", "Summarise order 1")
    for n in range(3):  # serial_llm_calls: independent questions, one after another
        await fake_llm("claude-demo", f"Describe product {n}")
    await fake_llm("claude-demo", policy + "Answer the customer.")  # uncached_large_prompt
    return {"answered": 7}
