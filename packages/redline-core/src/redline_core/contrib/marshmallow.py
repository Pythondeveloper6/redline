"""Marshmallow ``Schema.dump`` as ``serializer`` spans.

Call :func:`instrument_marshmallow` once at startup (an explicit choice, like the other
contrib patches). Only the *outermost* ``dump`` becomes a span – nested schemas
(``fields.Nested``) are part of their parent's time. Outside a traced request the wrapper
costs one ``ContextVar.get()``.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

from redline_core import record_span
from redline_core.recorder import current

__all__ = ["instrument_marshmallow", "uninstrument_marshmallow"]

_perf = time.perf_counter
_originals: dict[str, Any] = {}
_inside: ContextVar[bool] = ContextVar("redline_marshmallow", default=False)


def instrument_marshmallow() -> bool:
    """Patch ``marshmallow.Schema.dump``; ``False`` if marshmallow is missing. Idempotent."""
    try:
        import marshmallow
    except ImportError:
        return False
    if _originals:
        return True
    original = marshmallow.Schema.dump
    _originals["dump"] = original

    def dump(self: Any, obj: Any, *args: Any, **kwargs: Any) -> Any:
        if _inside.get() or current() is None:
            return original(self, obj, *args, **kwargs)
        token = _inside.set(True)
        start = _perf()
        try:
            return original(self, obj, *args, **kwargs)
        finally:
            _inside.reset(token)
            many = kwargs.get("many")
            if many is None:
                many = args[0] if args else self.many
            items = None
            if many:
                try:
                    items = len(obj)
                except TypeError:
                    items = None
            name = type(self).__name__
            record_span(
                "serializer",
                f"{name}.dump",
                start,
                _perf() - start,
                {"library": "marshmallow", "serializer": name, "items": items, "many": bool(many)},
            )

    dump.__wrapped__ = original  # type: ignore[attr-defined]
    marshmallow.Schema.dump = dump  # type: ignore[method-assign]
    return True


def uninstrument_marshmallow() -> None:
    """Undo :func:`instrument_marshmallow`."""
    if not _originals:
        return
    import marshmallow

    marshmallow.Schema.dump = _originals.pop("dump")  # type: ignore[method-assign]
