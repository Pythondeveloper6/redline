"""asyncpg queries as ``sql`` spans.

Call :func:`instrument_asyncpg` once at startup. It wraps ``Connection.execute``, ``fetch``,
``fetchrow``, ``fetchval`` and ``executemany``; pool methods delegate to these, so pooled
connections are covered. Statements are recorded in asyncpg's ``$1`` paramstyle. Outside a
traced request each wrapper is one ``ContextVar.get()``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import asyncpg

from redline_core import record_query
from redline_core.recorder import current

__all__ = ["instrument_asyncpg", "uninstrument_asyncpg"]

_perf = time.perf_counter
_originals: dict[str, Callable[..., Any]] = {}
_METHODS = ("execute", "fetch", "fetchrow", "fetchval", "executemany")


def _rows(method: str, result: Any) -> int | None:
    if method == "fetch" and isinstance(result, list):
        return len(result)
    if method == "fetchrow":
        return 0 if result is None else 1
    if method == "execute" and isinstance(result, str):
        tail = result.rsplit(" ", 1)[-1]  # command tag: "INSERT 0 5", "UPDATE 3"
        return int(tail) if tail.isdigit() else None
    return None


def _make(method: str, original: Callable[..., Any]) -> Callable[..., Any]:
    many = method == "executemany"

    async def wrapper(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        if current() is None:
            return await original(self, query, *args, **kwargs)
        start, result, ok = _perf(), None, False
        try:
            result = await original(self, query, *args, **kwargs)
            ok = True
            return result
        finally:
            # executemany(command, args, *, timeout): args[0] is the sequence of rows
            params = (args[0] if args else kwargs.get("args")) if many else args
            record_query(
                str(query),
                params,
                start=start,
                duration=_perf() - start,
                vendor="postgresql",
                rows=_rows(method, result) if ok else None,
                many=many,
            )

    wrapper.__name__ = method
    return wrapper


def instrument_asyncpg() -> bool:
    """Patch ``asyncpg.Connection``. Idempotent."""
    if _originals:
        return True
    for method in _METHODS:
        original = getattr(asyncpg.Connection, method)
        _originals[method] = original
        setattr(asyncpg.Connection, method, _make(method, original))
    return True


def uninstrument_asyncpg() -> None:
    """Undo :func:`instrument_asyncpg`."""
    for method in list(_originals):
        setattr(asyncpg.Connection, method, _originals.pop(method))
