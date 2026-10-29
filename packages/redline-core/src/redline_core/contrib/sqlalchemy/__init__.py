"""SQLAlchemy instrumentation (query spans), the EXPLAIN executor and the fix suggester.

Sync engines are instrumented directly; for an :class:`~sqlalchemy.ext.asyncio.AsyncEngine`
the listeners go on ``engine.sync_engine`` – the cursor-level events fire there for both.
The hooks cost one ``ContextVar.get()`` when no trace is active.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from functools import wraps
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

try:
    from sqlalchemy.ext.asyncio import AsyncEngine
except ImportError:  # no greenlet: sync-only install (e.g. a Flask app), so no async engines

    class AsyncEngine:  # type: ignore[no-redef]
        """Placeholder so ``isinstance(engine, AsyncEngine)`` is simply False."""


from redline_core import record_query
from redline_core.blocking import note_sync_call
from redline_core.recorder import _capture_failure, current, record_span
from redline_core.transactions import begin_transaction, end_transaction

from .suggest import SQLAlchemyFixSuggester

__all__ = [
    "EngineLike",
    "SQLAlchemyFixSuggester",
    "alias_for",
    "build_executor",
    "explain_executor_factory",
    "instrument_engine",
    "name_engines",
    "vendor_of",
]

EngineLike = Engine | AsyncEngine

_perf = time.perf_counter
_instrumented: weakref.WeakSet[Engine] = weakref.WeakSet()
_automatic: weakref.WeakSet[Engine] = weakref.WeakSet()
_registration_lock = threading.RLock()
_legacy_aliases: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_owners: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_auto_aliases: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_ATTR = "_redline_start"
_VENDORS = {"postgresql": "postgresql", "mysql": "mysql", "mariadb": "mysql", "sqlite": "sqlite"}


def _observer_failed():
    """Count capture failures without exposing exception messages or tracebacks."""
    try:
        builder = current()
        if builder is not None and builder.active and not builder.finished:
            with builder._lock:
                builder.capture_errors += 1
    except Exception:
        pass
    _capture_failure("Redline SQLAlchemy observer failed")


def _observer(callback):
    """An observer must never alter application database results or exceptions."""

    @wraps(callback)
    def safe(*args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except Exception:
            _observer_failed()
            return None

    return safe


def vendor_of(engine: EngineLike) -> str:
    from redline_core.vendors import get_vendor

    return get_vendor(engine.dialect.name).name


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


class _CountingCursor:
    """DBAPI cursor proxy that counts fetched rows into a span's ``data["rows"]``.

    Installed on the execution context from ``after_cursor_execute`` – SQLAlchemy builds
    its result from ``context.cursor`` right after that event – for read statements whose
    ``cursor.rowcount`` is unusable (sqlite, most async drivers). Everything but the three
    fetch methods is forwarded untouched. ``rows`` means "rows read so far".
    """

    __slots__ = ("_cursor", "_data", "_n")

    def __init__(self, cursor: Any, data: dict[str, Any]) -> None:
        self._cursor = cursor
        self._data = data
        self._n = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _CountingCursor.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._cursor, name, value)

    def fetchone(self) -> Any:
        row = self._cursor.fetchone()
        if row is not None:
            self._n += 1
        self._data["rows"] = self._n
        return row

    def fetchmany(self, *args: Any) -> Any:
        rows = self._cursor.fetchmany(*args)
        self._n += len(rows)
        self._data["rows"] = self._n
        return rows

    def fetchall(self) -> Any:
        rows = self._cursor.fetchall()
        self._n += len(rows)
        self._data["rows"] = self._n
        return rows


def _auto_alias(engine):
    with _registration_lock:
        if engine not in _auto_aliases:
            from redline_core.model import new_id

            _auto_aliases[engine] = "auto_" + new_id()[:16]
        return _auto_aliases[engine]


def _binding(engine, builder=None):
    builder = current() if builder is None else builder
    if builder is None or not builder.active or builder.finished:
        return None
    owner = _owners.get(engine)
    if owner is not None and builder.recorder in owner:
        return owner[builder.recorder], False
    if engine in _legacy_aliases:
        return _legacy_aliases[engine], False
    if builder.recorder.settings.instrument_engines == "all":
        return _auto_alias(engine), True
    return None


def instrument_engine(engine: EngineLike, alias: str = "default", *, recorder=None, _auto=False):
    """Install once, with optional recorder ownership for multi-application processes."""
    with _registration_lock:
        sync_engine = _sync(engine)
        if not _auto:
            if recorder is None:
                _legacy_aliases[sync_engine] = alias
            else:
                _owners.setdefault(sync_engine, weakref.WeakKeyDictionary())[recorder] = alias
        _install_engine(sync_engine, alias, _auto=_auto)


def _install_engine(sync_engine, alias, *, _auto=False):
    from sqlalchemy.orm import Session

    if not event.contains(Session, "loaded_as_persistent", _note_loaded_instance):
        event.listen(Session, "loaded_as_persistent", _note_loaded_instance)
    if sync_engine in _instrumented:
        if not _auto:
            _automatic.discard(sync_engine)
        return
    if _auto:
        _automatic.add(sync_engine)
    vendor = vendor_of(sync_engine)
    is_async = bool(getattr(sync_engine.dialect, "is_async", False))

    @_observer
    def before(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool):
        builder = current()
        binding = _binding(sync_engine, builder)
        if binding is None:
            return
        try:
            state = conn.info.setdefault("redline_lifecycle", {"born": None, "statements": 0})
            state["statements"] += 1
        except Exception:
            pass
        if context is not None and builder is not None:
            setattr(context, _ATTR, _perf())
            context._redline_owner = builder.id
            context._redline_alias, context._redline_auto = binding

    @_observer
    def after(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool):
        start = getattr(context, _ATTR, None) if context is not None else None
        if start is None:
            return
        duration = _perf() - start
        builder = current()
        if builder is None or getattr(context, "_redline_owner", None) != builder.id:
            return
        delattr(context, _ATTR)
        if statement.lstrip()[:6].upper() == "PRAGMA":
            return
        query_alias = context._redline_alias
        try:
            rows = cursor.rowcount
        except Exception:
            rows = None
        span = record_query(
            statement,
            parameters,
            start=start,
            duration=duration,
            db_alias=query_alias,
            vendor=vendor,
            rows=rows if isinstance(rows, int) and rows >= 0 else None,
            many=bool(many),
            capture_callsite=False,
        )
        if span is not None:
            span.callsite = _callsite(builder.recorder)
            span.data["auto_engine"] = bool(context._redline_auto)
            span.data["rowcount_raw"] = rows if isinstance(rows, int) else None
            _query_evidence(conn, span, statement)
            if span.data["rows"] is None and span.data.get("read_query") and context is not None:
                context.cursor = _CountingCursor(cursor, span.data)
            if not is_async:  # a sync driver called from a coroutine blocks the loop
                note_sync_call(span, "SQLAlchemy (sync)")

    @_observer
    def failed(exc_context: Any):
        context = exc_context.execution_context
        start = getattr(context, _ATTR, None) if context is not None else None
        builder = current()
        if (
            builder is not None
            and start is not None
            and getattr(context, "_redline_owner", None) == builder.id
        ):
            builder.sql_errors += 1
            query_alias = context._redline_alias
            span = record_query(
                exc_context.statement or "",
                exc_context.parameters,
                start=start,
                duration=_perf() - start,
                db_alias=query_alias,
                vendor=vendor,
                capture_callsite=False,
                many=bool(getattr(context, "executemany", False)),
            )
            if span is not None:
                span.data["auto_engine"] = bool(context._redline_auto)
                span.callsite = _callsite(builder.recorder)
                from redline_core.vendors import get_vendor

                details = get_vendor(vendor).classify_error(
                    exc_context.original_exception, disconnect=bool(exc_context.is_disconnect)
                )
                policy = builder.recorder.settings.capture_sql_params
                from redline_core.sql import is_explainable

                allowed = builder.recorder.settings.capture_error_details and (
                    policy is True
                    or (policy == "reads_only" and is_explainable(exc_context.statement or ""))
                )
                details["message"] = (
                    builder.recorder.redactor.text(str(exc_context.original_exception))[:500]
                    if allowed
                    else "[not captured]"
                )
                span.data["error"] = details
                span.data["explainable"] = False
                _query_evidence(exc_context.connection, span, exc_context.statement or "")
        if context is not None and hasattr(context, _ATTR):
            delattr(context, _ATTR)

    event.listen(sync_engine, "before_cursor_execute", before)
    event.listen(sync_engine, "after_cursor_execute", after)
    event.listen(sync_engine, "handle_error", failed)
    _instrument_transactions(sync_engine, alias)
    _instrument_pool(sync_engine, alias)
    _instrument_lifecycle(sync_engine, alias)
    _instrumented.add(sync_engine)


# ── transactions and pool checkout ─────────────────────────────────────────────────────

_TX = "_redline_tx"


def _instrument_transactions(sync_engine: Engine, alias: str) -> None:
    """``begin``/``commit``/``rollback`` → one ``transaction`` span per transaction.

    Savepoints (``begin_nested``) have their own events and are ignored.
    """

    @_observer
    def on_begin(conn: Any) -> None:
        binding = _binding(conn.engine)
        if binding is None:
            return
        if vendor_of(conn.engine) == "postgresql":
            conn.info["redline_session_before_tx"] = dict(conn.info.get("redline_session", {}))
        builder = current()
        if builder is None:
            return
        state = begin_transaction(builder, _callsite(builder.recorder))
        if state is not None:
            setattr(conn, _TX, state)

    def ender(outcome: Any) -> Callable[[Any], None]:
        @_observer
        def on_end(conn: Any) -> None:
            binding = _binding(conn.engine)
            if binding is None:
                return
            if outcome == "rollback" and vendor_of(conn.engine) == "postgresql":
                try:
                    conn.info["redline_session"] = conn.info.pop("redline_session_before_tx", {})
                except Exception:
                    pass
            state = conn.__dict__.pop(_TX, None)
            if state is not None:
                end_transaction(state, db_alias=binding[0], outcome=outcome)

        return on_end

    event.listen(sync_engine, "begin", on_begin)
    event.listen(sync_engine, "commit", ender("commit"))
    event.listen(sync_engine, "rollback", ender("rollback"))


#: Pool checkouts shorter than this are not recorded (seconds).
POOL_WAIT_MIN = 0.001


def _instrument_pool(sync_engine: Engine, alias: str) -> None:
    """Time ``Engine.raw_connection`` (the pool checkout) → ``pool_wait`` spans ≥ 1 ms.

    The pool has no "checkout requested" event, so the engine's own ``raw_connection`` is
    wrapped on the instance (``Connection`` calls it for every ``engine.connect()``). The
    time includes opening a new DBAPI connection when the pool has none to hand out.
    """
    original = sync_engine.raw_connection

    def raw_connection(*args: Any, **kwargs: Any) -> Any:
        binding = start = None
        try:
            binding = _binding(sync_engine)
            if binding is not None:
                start = _perf()
        except Exception:
            _observer_failed()
        # This is the application's database operation: propagate its original error
        # and never call it twice when only timing or recording fails.
        dbapi_conn = original(*args, **kwargs)
        if binding is None or start is None:
            return dbapi_conn
        try:
            duration = _perf() - start
            if duration >= POOL_WAIT_MIN:
                builder = current()
                pool = sync_engine.pool
                record_span(
                    "pool_wait",
                    f"pool checkout {binding[0]}",
                    start,
                    duration,
                    {
                        "db_alias": binding[0],
                        "pool_size": _pool_stat(pool, "size"),
                        "checked_out": _pool_stat(pool, "checkedout"),
                    },
                    capture_callsite=False,
                    callsite=_callsite(builder.recorder) if builder is not None else None,
                )
        except Exception:
            _observer_failed()
        return dbapi_conn

    sync_engine.raw_connection = raw_connection  # type: ignore[method-assign]


def _pool_stat(pool: Any, name: str) -> int | None:
    value = getattr(pool, name, None)
    try:
        value = value() if callable(value) else value
    except Exception:
        return None
    return value if isinstance(value, int) else None


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

        async def estimated_async(sql, params):
            async with engine.connect() as conn:
                try:
                    await conn.exec_driver_sql("SET SHOWPLAN_XML ON")
                    result = await conn.exec_driver_sql(sql, _positional(params))
                    return [tuple(r) for r in result.fetchall()]
                finally:
                    try:
                        await conn.exec_driver_sql("SET SHOWPLAN_XML OFF")
                    except BaseException:
                        await conn.invalidate()
                        raise
                    finally:
                        await conn.rollback()

        def async_plan(sql, params):
            loop = get_loop()
            if loop is None or not loop.is_running():
                return asyncio.run(estimated_async(sql, params))
            return asyncio.run_coroutine_threadsafe(estimated_async(sql, params), loop).result(30)

        async_executor.mssql_estimated_plan = async_plan
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

    def estimated(sql, params):
        with engine.connect() as conn:
            try:
                conn.exec_driver_sql("SET SHOWPLAN_XML ON")
                result = conn.exec_driver_sql(sql, _positional(params))
                return [tuple(r) for r in result.fetchall()]
            finally:
                try:
                    conn.exec_driver_sql("SET SHOWPLAN_XML OFF")
                except BaseException:
                    conn.invalidate()
                    raise
                finally:
                    conn.rollback()

    def actual(sql, params):
        from redline_core.mssql_actual import collect

        return collect(engine, sql, params)

    executor.mssql_actual_plan = actual
    executor.mssql_estimated_plan = estimated
    return executor


#: Contract name for :func:`build_executor`.
explain_executor_factory = build_executor


@_observer
def _note_loaded_instance(session, instance):
    from redline_core.orm import note_instance

    note_instance()


def _query_evidence(conn, span, statement):
    from redline_core.sql import comment_placeholders, effective_kinds

    if span.data.get("params_captured") is False and span.callsite is not None:
        from dataclasses import replace

        span.callsite = replace(span.callsite, code=None)
    span.data["statement_kinds"] = list(effective_kinds(statement))
    span.data["comment_placeholders"] = comment_placeholders(statement, conn.dialect.paramstyle)
    try:
        info = conn.info
        info["redline_vendor"] = span.data.get("vendor", "other")
        info["redline_alias"] = span.data.get("db_alias", "default")
        state = info.setdefault("redline_lifecycle", {"born": None, "statements": 0})
        span.data["connection"] = {
            "age_s": round(max(0.0, _perf() - state["born"]), 3)
            if state["born"] is not None
            else None,
            "statement_number": state["statements"],
            "pool_recycle": getattr(conn.engine.pool, "_recycle", None),
            "pool_pre_ping": bool(getattr(conn.engine.pool, "_pre_ping", False)),
        }
        from redline_core.connection_health import evidence

        span.data["connection"].update(
            evidence(
                conn.engine.pool,
                span.data["connection"]["age_s"],
                state["statements"],
                span.data.get("error"),
            )
        )
        if not span.data.get("error"):
            from redline_core.session_state import observe

            observe(info, statement, span)
    except Exception:
        _observer_failed()


def _instrument_lifecycle(engine, alias):
    @_observer
    def connected(dbapi, record):
        from redline_core.connection_health import connected as note_connected
        from redline_core.connection_health import register

        note_connected(engine.pool)
        register(engine.pool, alias=alias, vendor=engine.dialect.name)
        record.info["redline_lifecycle"] = {"born": _perf(), "statements": 0}

    @_observer
    def checked_in(dbapi, record):
        from redline_core.session_state import checked_in

        binding = _binding(engine)
        if binding is not None:
            checked_in(record.info, binding[0])

    event.listen(engine.pool, "connect", connected)
    event.listen(engine.pool, "checkin", checked_in)

    @_observer
    def invalidated(dbapi, record, exc):
        binding = _binding(engine)
        if binding is None:
            return
        record.info["redline_vendor"] = vendor_of(engine)
        record.info["redline_alias"] = binding[0]
        _pool_invalidated(dbapi, record, exc, registered=True)

    event.listen(engine.pool, "invalidate", invalidated)


_all_installed = False


def _install_all_engines():
    """Install one lazy class hook; only active recorders opting into all are observed."""
    global _all_installed
    if _all_installed:
        return
    from sqlalchemy.pool import Pool

    event.listen(Pool, "connect", _global_connected)
    event.listen(Pool, "invalidate", _pool_invalidated)
    event.listen(Engine, "engine_connect", _discover_active_engine)
    _all_installed = True


@_observer
def _discover_active_engine(conn):
    builder = current()
    if builder is not None and builder.recorder.settings.instrument_engines == "all":
        engine = conn.engine
        instrument_engine(engine, _auto_alias(engine), _auto=True)


@_observer
def _global_connected(dbapi, record):
    record.info["redline_lifecycle"] = {"born": _perf(), "statements": 0}


@_observer
def _pool_invalidated(dbapi, record, exc, *, registered=False):
    builder = current()
    if builder is None or builder.finished or exc is None:
        return
    if not registered and builder.recorder.settings.instrument_engines != "all":
        return
    info = record.info
    identity = (builder.id, id(exc))
    if info.get("redline_invalidation") == identity:
        return
    info["redline_invalidation"] = identity
    builder.connection_errors += 1
    from redline_core.vendors import get_vendor

    vendor = info.get("redline_vendor", "other")
    if vendor == "other" and type(dbapi).__module__.startswith("pyodbc"):
        vendor = "mssql"
    error = get_vendor(vendor).classify_error(exc, disconnect=True)
    record_span(
        "pool_wait",
        "Database connection invalidated",
        _perf(),
        0,
        {"db_alias": info.get("redline_alias", "unregistered"), "connection_error": error},
        capture_callsite=False,
    )


def instrument_all_engines():
    with _registration_lock:
        _install_all_engines()


def _reset_registration_lock():
    global _registration_lock
    _registration_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_registration_lock)
