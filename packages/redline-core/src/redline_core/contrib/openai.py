"""OpenAI SDK chat completions as ``llm_call`` spans (model, tokens, timing; no prompt text).

Call :func:`instrument_openai` once at startup. It wraps ``Completions.create`` and
``AsyncCompletions.create``. Streaming calls are recorded without token counts.
"""

from __future__ import annotations

from typing import Any

from ._llm import instrument_class, restore_class

__all__ = ["instrument_openai", "uninstrument_openai"]

_originals: dict[Any, Any] = {}


def _usage(result: Any) -> dict[str, int | None]:
    u = result.usage
    details = getattr(u, "prompt_tokens_details", None)
    return {
        "input_tokens": getattr(u, "prompt_tokens", None),
        "output_tokens": getattr(u, "completion_tokens", None),
        "cache_read_tokens": getattr(details, "cached_tokens", None),
    }


def instrument_openai() -> bool:
    """Patch the SDK; ``False`` if it is not installed. Idempotent."""
    try:
        from openai.resources.chat import completions
    except ImportError:
        return False
    if _originals:
        return True
    for name in ("Completions", "AsyncCompletions"):
        cls = getattr(completions, name, None)
        if cls is not None:
            instrument_class(cls, "openai", _usage, _originals)
    return True


def uninstrument_openai() -> None:
    restore_class(_originals)
