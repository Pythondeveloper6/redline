"""psycopg 3 queries (without an ORM) as ``sql`` spans.

Call :func:`instrument_psycopg` once at startup. It wraps ``execute`` and ``executemany`` of
``psycopg.Cursor`` and ``psycopg.AsyncCursor``, which is what ``conn.execute()`` and
``conn.cursor().execute()`` both use. The patch is process-wide, so the optional connection
argument is accepted only for call-site clarity. Server-side cursors are not covered.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import psycopg

from redline_core import record_query
from redline_core.recorder import current

__all__ = ["instrument_psycopg", "uninstrument_psycopg"]

_perf = time.perf_counter
_originals: dict[str, Callable[..., Any]] = {}
_TARGETS = (
    ("Cursor", "execute"),
    ("Cursor", "executemany"),
    ("AsyncCursor", "execute"),
    ("AsyncCursor", "executemany"),
)


def _text(query: Any) -> str:
    if isinstance(query, str):
        return query
    if isinstance(query, bytes):
        return query.decode("utf-8", "replace")
    try:  # psycopg.sql.Composable
        return str(query.as_string(None))
    except Exception:
        return str(query)


def _record(cursor: Any, query: Any, params: Any, start: float, many: bool) -> None:
    try:
        rows = cursor.rowcount
    except Exception:
        rows = None
    record_query(
        _text(query),
        params,
        start=start,
        duration=_perf() - start,
        vendor="postgresql",
        rows=rows if isinstance(rows, int) and rows >= 0 else None,
        many=many,
    )


def _make(method: str, original: Callable[..., Any], is_async: bool) -> Callable[..., Any]:
    many = method == "executemany"

    if is_async:

        async def awrapper(self: Any, query: Any, params: Any = None, *a: Any, **kw: Any) -> Any:
            if current() is None:
                return await original(self, query, params, *a, **kw)
            start = _perf()
            try:
                return await original(self, query, params, *a, **kw)
            finally:
                _record(self, query, params, start, many)

        return awrapper

    def wrapper(self: Any, query: Any, params: Any = None, *a: Any, **kw: Any) -> Any:
        if current() is None:
            return original(self, query, params, *a, **kw)
        start = _perf()
        try:
            return original(self, query, params, *a, **kw)
        finally:
            _record(self, query, params, start, many)

    return wrapper


def instrument_psycopg(conn: Any = None) -> bool:
    """Patch psycopg 3 cursors (process-wide; ``conn`` is ignored). Idempotent."""
    if _originals:
        return True
    for cls_name, method in _TARGETS:
        cls = getattr(psycopg, cls_name)
        original = getattr(cls, method)
        _originals[f"{cls_name}.{method}"] = original
        setattr(cls, method, _make(method, original, cls_name == "AsyncCursor"))
    return True


def uninstrument_psycopg() -> None:
    """Undo :func:`instrument_psycopg`."""
    for key in list(_originals):
        cls_name, method = key.split(".")
        setattr(getattr(psycopg, cls_name), method, _originals.pop(key))
