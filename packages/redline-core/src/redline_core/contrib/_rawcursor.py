"""Recording proxies for DB-API connections and cursors used without an ORM engine.

Used by the SQLAlchemy integration (``Engine.raw_connection()``) and by
:func:`redline_core.contrib.dbapi.instrument_dbapi`. Spans carry ``source="raw_cursor"``.
Everything the proxies do not record is forwarded to the driver object untouched, and driver
errors propagate unchanged.
"""

from __future__ import annotations

import time
from typing import Any

from redline_core.recorder import current, record_query

__all__ = ["RawConnection", "RawCursor", "wrap_connection"]

_perf = time.perf_counter
SOURCE = "raw_cursor"


def _text(query: Any) -> str:
    if isinstance(query, str):
        return query
    if isinstance(query, bytes):
        return query.decode("utf-8", "replace")
    try:  # psycopg.sql.Composable
        return str(query.as_string(None))
    except Exception:
        return str(query)


def _batch_size(params: Any) -> int | None:
    try:
        return len(params)
    except Exception:
        return None


def _record(
    cursor: Any,
    vendor: str,
    alias: str,
    sql: str,
    params: Any,
    start: float,
    *,
    many: bool,
    error: BaseException | None,
) -> Any:
    builder = current()
    if builder is None or builder.finished:
        return None
    try:
        rows = cursor.rowcount
    except Exception:
        rows = None
    span = record_query(
        sql,
        params,
        start=start,
        duration=_perf() - start,
        db_alias=alias,
        vendor=vendor,
        rows=rows if isinstance(rows, int) and rows >= 0 else None,
        many=many,
        honor_ignore=error is None,
    )
    if span is None:
        return None
    span.data["source"] = SOURCE
    if many:
        span.data["batch_size"] = _batch_size(params)
    if error is not None:
        builder.sql_errors += 1
        span.data["error"] = {"type": type(error).__name__, "message": "[not captured]"}
        span.data["explainable"] = False
    return span


class RawCursor:
    """Cursor proxy that records ``execute``, ``executemany`` and ``callproc``."""

    __slots__ = ("_alias", "_cursor", "_last", "_vendor")

    def __init__(self, cursor: Any, vendor: str, alias: str) -> None:
        object.__setattr__(self, "_cursor", cursor)
        object.__setattr__(self, "_vendor", vendor)
        object.__setattr__(self, "_alias", alias)
        object.__setattr__(self, "_last", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in RawCursor.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._cursor, name, value)

    def __iter__(self) -> Any:
        return iter(self._cursor)

    def __enter__(self) -> Any:
        result = self._cursor.__enter__()
        return self if result is self._cursor else result

    def __exit__(self, *exc: Any) -> Any:
        return self._cursor.__exit__(*exc)

    def _run(self, method: str, sql: Any, params: Any, args: tuple, kw: dict, many: bool) -> Any:
        call = getattr(self._cursor, method)
        has_params = params is not _MISSING
        if current() is None:
            return call(sql, params, *args, **kw) if has_params else call(sql, *args, **kw)
        start = _perf()
        error: BaseException | None = None
        try:
            return call(sql, params, *args, **kw) if has_params else call(sql, *args, **kw)
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                span = _record(
                    self._cursor,
                    self._vendor,
                    self._alias,
                    _text(sql),
                    None if not has_params else params,
                    start,
                    many=many,
                    error=error,
                )
                object.__setattr__(self, "_last", span)
            except Exception:  # recording must never break the application's query
                pass

    def execute(self, sql: Any, params: Any = None, *args: Any, **kw: Any) -> Any:
        result = self._run(
            "execute", sql, params if params is not None else _MISSING, args, kw, False
        )
        return self if result is self._cursor else result

    def executemany(self, sql: Any, params: Any, *args: Any, **kw: Any) -> Any:
        result = self._run("executemany", sql, params, args, kw, True)
        return self if result is self._cursor else result

    def callproc(self, name: str, params: Any = None, *args: Any, **kw: Any) -> Any:
        call = self._cursor.callproc
        if current() is None:
            return call(name, params, *args, **kw) if params is not None else call(name)
        start = _perf()
        error: BaseException | None = None
        try:
            return call(name, params, *args, **kw) if params is not None else call(name)
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                span = _record(
                    self._cursor,
                    self._vendor,
                    self._alias,
                    f"CALL {name}",
                    params,
                    start,
                    many=False,
                    error=error,
                )
                object.__setattr__(self, "_last", span)
            except Exception:
                pass

    def nextset(self) -> Any:
        """Time spent advancing to the next result set is added to the last span."""
        start = _perf()
        try:
            return self._cursor.nextset()
        finally:
            span = self._last
            if span is not None:
                try:
                    span.data["nextset_s"] = round(
                        float(span.data.get("nextset_s") or 0.0) + (_perf() - start), 6
                    )
                except Exception:
                    pass


class _Missing:
    __slots__ = ()


_MISSING: Any = _Missing()


class RawConnection:
    """Connection proxy whose ``cursor()`` (and ``execute`` shortcuts) record statements."""

    __slots__ = ("_alias", "_conn", "_vendor")

    def __init__(self, conn: Any, vendor: str, alias: str) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_vendor", vendor)
        object.__setattr__(self, "_alias", alias)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._conn, name)
        if name in ("execute", "executemany"):  # sqlite3-style connection shortcuts
            return self._shortcut(name)
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        if name in RawConnection.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    def _wrap(self, cursor: Any) -> Any:
        return RawCursor(cursor, self._vendor, self._alias)

    def cursor(self, *args: Any, **kw: Any) -> Any:
        return self._wrap(self._conn.cursor(*args, **kw))

    def _shortcut(self, name: str) -> Any:
        def run(sql: Any, *args: Any, **kw: Any) -> Any:
            cursor = self._wrap(self._conn.cursor())
            getattr(cursor, name)(sql, *args, **kw)
            return cursor

        return run

    def __enter__(self) -> Any:
        result = self._conn.__enter__()
        return self if result is self._conn else result

    def __exit__(self, *exc: Any) -> Any:
        return self._conn.__exit__(*exc)


def wrap_connection(conn: Any, vendor: str, alias: str = "default") -> Any:
    """Return ``conn`` unchanged when already wrapped, else a recording proxy."""
    if isinstance(conn, RawConnection):
        return conn
    return RawConnection(conn, vendor, alias)
