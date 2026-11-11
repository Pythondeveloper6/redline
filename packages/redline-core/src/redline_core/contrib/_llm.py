"""Shared recording for LLM SDK wrappers.

Only metadata is stored: provider, model, token counts, status, whether it streamed, a short
hash of the prompt (to find identical calls) and its size. Prompt text, completions, tool
arguments and exception messages are never recorded.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

from redline_core import record_span
from redline_core.recorder import current

__all__ = ["fingerprint", "instrument_class", "restore_class"]

_perf = time.perf_counter
_PROMPT_KEYS = ("system", "messages", "input", "instructions", "tools", "prompt")
_MAX_HASHED_CHARS = 2_000_000


def fingerprint(kwargs: dict[str, Any]) -> tuple[str, int]:
    """``(16-hex hash, size in characters)`` of the prompt-bearing arguments."""
    try:
        text = json.dumps({k: kwargs.get(k) for k in _PROMPT_KEYS}, default=str, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        text = repr(sorted(kwargs))
    return hashlib.sha256(text[:_MAX_HASHED_CHARS].encode("utf-8", "replace")).hexdigest()[
        :16
    ], len(text)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _record(
    provider: str,
    kwargs: dict[str, Any],
    start: float,
    result: Any,
    exc: BaseException | None,
    read_usage: Callable[[Any], dict[str, int | None]],
) -> None:
    stream = bool(kwargs.get("stream"))
    digest, chars = fingerprint(kwargs)
    data: dict[str, Any] = {
        "provider": provider,
        "model": str(kwargs.get("model") or "unknown")[:100],
        "status": "error" if exc is not None else "ok",
        "stream": stream,
        "prompt_hash": digest,
        "prompt_chars": chars,
        "max_tokens": _int(kwargs.get("max_tokens") or kwargs.get("max_output_tokens")),
    }
    if exc is not None:
        data["error_type"] = type(exc).__name__
    elif not stream:
        try:
            data.update({k: _int(v) for k, v in read_usage(result).items()})
        except Exception:  # an unfamiliar response shape must never break the call
            pass
    record_span("llm_call", f"{provider} {data['model']}", start, _perf() - start, data)


def instrument_class(
    cls: type,
    provider: str,
    read_usage: Callable[[Any], dict[str, int | None]],
    originals: dict[Any, Any],
) -> None:
    """Wrap ``cls.create`` (sync or async), remembering the original in ``originals``."""
    import inspect

    original = cls.create  # type: ignore[attr-defined]
    originals[cls] = original
    if inspect.iscoroutinefunction(original):

        async def create_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            if current() is None:
                return await original(self, *args, **kwargs)
            start = _perf()
            try:
                result = await original(self, *args, **kwargs)
            except BaseException as exc:
                _record(provider, kwargs, start, None, exc, read_usage)
                raise
            _record(provider, kwargs, start, result, None, read_usage)
            return result

        cls.create = create_async  # type: ignore[attr-defined]
    else:

        def create(self: Any, *args: Any, **kwargs: Any) -> Any:
            if current() is None:
                return original(self, *args, **kwargs)
            start = _perf()
            try:
                result = original(self, *args, **kwargs)
            except BaseException as exc:
                _record(provider, kwargs, start, None, exc, read_usage)
                raise
            _record(provider, kwargs, start, result, None, read_usage)
            return result

        cls.create = create  # type: ignore[attr-defined]


def restore_class(originals: dict[Any, Any]) -> None:
    for cls, original in originals.items():
        cls.create = original
    originals.clear()
