"""SQLAlchemy instrumentation (query spans) and the EXPLAIN executor.

Sync engines are instrumented directly; for an :class:`~sqlalchemy.ext.asyncio.AsyncEngine`
the listeners go on ``engine.sync_engine`` – the cursor-level events fire there for both.
The hooks cost one ``ContextVar.get()`` when no trace is active.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from redline_core import record_query
from redline_core.recorder import current

logger = logging.getLogger("redline")

__all__ = [
    "EngineLike",
    "alias_for",
    "build_executor",
    "instrument_engine",
    "name_engines",
    "vendor_of",
]

EngineLike = Engine | AsyncEngine

_perf = time.perf_counter
_instrumented: weakref.WeakSet[Engine] = weakref.WeakSet()
_ATTR = "_redline_start"
_VENDORS = {"postgresql": "postgresql", "mysql": "mysql", "mariadb": "mysql", "sqlite": "sqlite"}


def vendor_of(engine: EngineLike) -> str:
    return _VENDORS.get(engine.dialect.name, "other")


def _sync(engine: EngineLike) -> Engine:
    return engine.sync_engine if isinstance(engine, AsyncEngine) else engine


def name_engines(engine: Any) -> dict[str, EngineLike]:
    """Normalise ``install(engine=...)`` into ``{db_alias: engine}``.

    A single engine is ``"default"``. A mapping keeps its keys. A list names the first
    engine ``"default"`` and the rest after their database (``analytics`` for
    ``.../analytics.db``), falling back to ``db2``, ``db3``…
    """
    if engine is None:
        return {}
    if isinstance(engine, Mapping):
        return {str(k): v for k, v in engine.items()}
    engines: Sequence[EngineLike] = (
        list(engine) if isinstance(engine, (list, tuple, set)) else [engine]
    )
    named: dict[str, EngineLike] = {}
    for i, eng in enumerate(engines):
        named["default" if i == 0 else alias_for(eng, named)] = eng
    return named


def alias_for(engine: EngineLike, taken: Mapping[str, Any]) -> str:
    """A db alias from the engine's database name, unique among ``taken``."""
    db = _sync(engine).url.database or ""
    alias = db.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if not alias or alias in taken or alias == ":memory:":
        alias = f"db{len(taken) + 1}"
    return alias


def _callsite(recorder: Any) -> Any:
    """Innermost project frame – also across the greenlet boundary of AsyncSession."""
    capturer = recorder.capturer
    frame = sys._getframe(2)
    callsite = capturer.capture(frame=frame)
    if callsite is not None:
        return callsite
    try:
        import greenlet
    except ImportError:  # pragma: no cover - asyncio SQLAlchemy needs greenlet
        return None
    parent = greenlet.getcurrent().parent
    while parent is not None:
        top = getattr(parent, "gr_frame", None)
        if top is not None:
            callsite = capturer.capture(frame=top)
            if callsite is not None:
                return callsite
        parent = parent.parent
    return None


def instrument_engine(engine: EngineLike, alias: str = "default") -> None:
    """Record every statement executed on ``engine`` as a ``sql`` span (idempotent)."""
    sync_engine = _sync(engine)
    if sync_engine in _instrumented:
        return
    _instrumented.add(sync_engine)
    vendor = vendor_of(sync_engine)

    def before(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool):
        if context is not None and current() is not None:
            setattr(context, _ATTR, _perf())

    def after(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool):
        start = getattr(context, _ATTR, None) if context is not None else None
        if start is None:
            return
        duration = _perf() - start
        builder = current()
        if builder is None or statement.lstrip()[:6].upper() == "PRAGMA":
            return
        try:
            rows = cursor.rowcount
        except Exception:
            rows = None
        span = record_query(
            statement,
            parameters,
            start=start,
            duration=duration,
            db_alias=alias,
            vendor=vendor,
            rows=rows if isinstance(rows, int) and rows >= 0 else None,
            many=bool(many),
            capture_callsite=False,
        )
        if span is not None:
            span.callsite = _callsite(builder.recorder)

    def failed(exc_context: Any):
        context = exc_context.execution_context
        if context is not None and hasattr(context, _ATTR):
            delattr(context, _ATTR)

    event.listen(sync_engine, "before_cursor_execute", before)
    event.listen(sync_engine, "after_cursor_execute", after)
    event.listen(sync_engine, "handle_error", failed)


# ── EXPLAIN ────────────────────────────────────────────────────────────────────────────


def _positional(params: Any) -> Any:
    """Captured params are JSON-safe lists; drivers want tuples (lists mean executemany)."""
    if isinstance(params, list):
        return tuple(params)
    return params if params is not None else ()


def build_executor(
    engine: EngineLike, vendor: str, get_loop: Callable[[], asyncio.AbstractEventLoop | None]
) -> Callable[[str, Any], list[tuple[Any, ...]]]:
    """``executor(sql, params) -> rows`` running in a transaction that is always rolled back.

    The captured SQL is in the *driver's* paramstyle, so it goes through
    ``exec_driver_sql`` untouched. Sync engines run on the calling (worker) thread; async
    engines run on the application's event loop, where their connections belong.
    """
    if isinstance(engine, AsyncEngine):

        async def run_async(sql: str, params: Any) -> list[tuple[Any, ...]]:
            async with engine.connect() as conn:
                try:
                    if vendor == "postgresql":
                        await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    result = await conn.exec_driver_sql(sql, _positional(params))
                    return [tuple(r) for r in result.fetchall()] if result.returns_rows else []
                finally:
                    await conn.rollback()

        def async_executor(sql: str, params: Any) -> list[tuple[Any, ...]]:
            loop = get_loop()
            if loop is None or not loop.is_running():
                return asyncio.run(run_async(sql, params))
            return asyncio.run_coroutine_threadsafe(run_async(sql, params), loop).result(30)

        return async_executor

    def executor(sql: str, params: Any) -> list[tuple[Any, ...]]:
        with engine.connect() as conn:
            try:
                if vendor == "postgresql":
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                result = conn.exec_driver_sql(sql, _positional(params))
                return [tuple(r) for r in result.fetchall()] if result.returns_rows else []
            finally:
                conn.rollback()

    return executor
