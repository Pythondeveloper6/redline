"""Response serialization as ``serializer`` spans.

FastAPI validates and serializes the endpoint's return value in
``fastapi.routing.serialize_response`` (response-model validation + Pydantic dump, or
``jsonable_encoder`` without a response model). That one coroutine is wrapped: the span
covers all of it, nested models included. The wrapper is looked up as a module global by
FastAPI's request handler, so patching the attribute is enough; it is defensive about the
signature (keyword-only today, but only ``field``/``response_content`` are read, by name)
and costs one ``ContextVar.get()`` outside a traced request.
"""

from __future__ import annotations

import functools
import time
from typing import Any, get_args, get_origin

from redline_core import record_span
from redline_core.recorder import current

__all__ = ["instrument_serialization", "uninstrument_serialization"]

_perf = time.perf_counter
_originals: dict[str, Any] = {}
_SEQUENCES = (list, tuple, set, frozenset)


def _type_name(annotation: Any) -> str | None:
    """``Order`` for ``Order``, ``list[Order]``, ``Optional[Order]``…"""
    if annotation is None:
        return None
    args = [a for a in get_args(annotation) if a is not type(None)]
    if get_origin(annotation) is not None and args:
        return _type_name(args[0])
    return getattr(annotation, "__name__", None)


def _describe(field: Any, content: Any) -> tuple[str, int | None]:
    """``(model name, item count or None)`` for one serialization."""
    items = len(content) if isinstance(content, _SEQUENCES) else None
    annotation = getattr(field, "type_", None)
    if annotation is None:
        info = getattr(field, "field_info", None)
        annotation = getattr(info, "annotation", None)
    name = _type_name(annotation)
    if name is None:
        sample = content[0] if items else content
        name = type(sample).__name__ if items != 0 else "list"
    return name, items


def instrument_serialization() -> bool:
    """Patch ``fastapi.routing.serialize_response``; ``False`` if it is not there. Idempotent."""
    try:
        from fastapi import routing
    except ImportError:  # pragma: no cover - fastapi is this package's dependency
        return False
    original = getattr(routing, "serialize_response", None)
    if original is None:
        return False
    if _originals:
        return True
    _originals["serialize_response"] = original

    @functools.wraps(original)
    async def serialize_response(*args: Any, **kwargs: Any) -> Any:
        if current() is None:
            return await original(*args, **kwargs)
        start = _perf()
        try:
            return await original(*args, **kwargs)
        finally:
            duration = _perf() - start
            try:
                name, items = _describe(kwargs.get("field"), kwargs.get("response_content"))
            except Exception:
                name, items = "response", None
            record_span(
                "serializer",
                f"serialize {name}",
                start,
                duration,
                {
                    "library": "pydantic",
                    "serializer": name,
                    "items": items,
                    "many": items is not None,
                },
            )

    routing.serialize_response = serialize_response
    return True


def uninstrument_serialization() -> None:
    """Undo :func:`instrument_serialization`."""
    if not _originals:
        return
    from fastapi import routing

    routing.serialize_response = _originals.pop("serialize_response")
