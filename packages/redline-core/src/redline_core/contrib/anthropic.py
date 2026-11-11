"""Anthropic SDK calls as ``llm_call`` spans (model, tokens, timing; never prompt text).

Call :func:`instrument_anthropic` once at startup. It wraps ``Messages.create`` and
``AsyncMessages.create``. Streaming calls are recorded without token counts because usage is
only known when the stream ends.
"""

from __future__ import annotations

from typing import Any

from ._llm import instrument_class, restore_class

__all__ = ["instrument_anthropic", "uninstrument_anthropic"]

_originals: dict[Any, Any] = {}


def _usage(result: Any) -> dict[str, int | None]:
    u = result.usage
    return {
        "input_tokens": getattr(u, "input_tokens", None),
        "output_tokens": getattr(u, "output_tokens", None),
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", None),
        "cache_creation_tokens": getattr(u, "cache_creation_input_tokens", None),
    }


def instrument_anthropic() -> bool:
    """Patch the SDK; ``False`` if it is not installed. Idempotent."""
    try:
        from anthropic.resources import messages
    except ImportError:
        return False
    if _originals:
        return True
    for name in ("Messages", "AsyncMessages"):
        cls = getattr(messages, name, None)
        if cls is not None:
            instrument_class(cls, "anthropic", _usage, _originals)
    return True


def uninstrument_anthropic() -> None:
    restore_class(_originals)
