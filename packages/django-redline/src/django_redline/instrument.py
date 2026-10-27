"""Hooks into Django: SQL, cache, templates and the view call.

Everything here is installed once from :meth:`RedlineConfig.ready` and is a cheap
pass-through when no trace is active (one ``ContextVar.get()`` and a ``None`` check).

* **SQL** – a ``connection.execute_wrapper`` appended to every connection's
  ``execute_wrappers`` (through ``connection_created`` and for connections that are
  already open). Redline's own store is a separate ``sqlite3`` file, never a Django
  connection, so it can never record itself.
* **Cache** – each cache backend instance created by ``django.core.cache.caches`` gets
  its public methods wrapped into ``cache`` spans with ``op``, ``key`` and ``hit``.
* **Templates** – ``django.template.base.Template.render`` (top-level templates and
  ``{% include %}``) and the Jinja2 backend become ``template`` spans; queries run while
  rendering nest under them.
* **View** – ``BaseHandler.make_view_atomic`` (called by both the sync and the async
  handler right before the view) wraps the view in a ``view`` span and remembers where
  the view is defined, so queries issued from library code – a generic ``ListView``, a
  DRF viewset – are still attributed to the project view that caused them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import inspect
import linecache
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import Any

from django.core.handlers.base import BaseHandler
from django.db import connections
from django.db.backends.signals import connection_created

from redline_core.blocking import note_sync_call
from redline_core.callsite import MAX_STACK
from redline_core.model import Callsite, Frame
from redline_core.recorder import SpanContext, current, record_query, record_span

from ._drf import install_drf

__all__ = [
    "ViewFrame",
    "install",
    "install_cache_handler",
    "install_connection",
    "suppress",
]

_perf = time.perf_counter
_installed = False

#: Set while Redline itself talks to the database (EXPLAIN), so nothing is recorded.
_suppressed: ContextVar[bool] = ContextVar("redline_suppressed", default=False)
#: A one-slot holder for the project view handling the current request (for callsite
#: attribution). The middleware creates it; the view hook fills it. A mutable holder,
#: because the view may run in a copied context (``sync_to_async``) while a lazily
#: rendered ``TemplateResponse`` runs its queries after the view has returned.
_view: ContextVar[list[ViewFrame | None] | None] = ContextVar("redline_view", default=None)


def begin_request() -> Any:
    """Create the view holder for a request; pass the token to :func:`end_request`."""
    return _view.set([None])


def end_request(token: Any) -> None:
    try:
        _view.reset(token)
    except ValueError:  # finished in another context
        _view.set(None)


#: True while inside a recorded cache operation (nested calls are not recorded twice).
_in_cache: ContextVar[bool] = ContextVar("redline_in_cache", default=False)


@contextlib.contextmanager
def suppress() -> Iterator[None]:
    """Do not record SQL/cache spans inside this block."""
    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)


def install() -> None:
    """Install every hook (idempotent)."""
    global _installed
    if _installed:
        return
    _installed = True
    connection_created.connect(_on_connection_created, dispatch_uid="django_redline.sql")
    for conn in connections.all(initialized_only=True):
        install_connection(conn)
    install_cache_handler()
    _install_templates()
    _install_view_hook()
    install_drf()
    from .transactions import install_atomic

    install_atomic()


# ── SQL ─────────────────────────────────────────────────────────────────────────────


class QueryRecorder:
    """The ``execute_wrapper`` appended to every database connection."""

    __slots__ = ()

    def __call__(self, execute: Callable[..., Any], sql: Any, params: Any, many: bool, context):
        if current() is None or _suppressed.get():
            _detach_counter(context.get("cursor"))  # a reused cursor stops counting
            return execute(sql, params, many, context)
        start = _perf()
        try:
            return execute(sql, params, many, context)
        finally:
            duration = _perf() - start
            _record(sql, params, many, context, start, duration)


def _record(sql: Any, params: Any, many: bool, context: dict, start: float, duration: float):
    connection = context.get("connection")
    rows = None
    cursor = context.get("cursor")
    if cursor is not None:
        try:
            rowcount = cursor.rowcount
            rows = rowcount if isinstance(rowcount, int) and rowcount >= 0 else None
        except Exception:
            rows = None
    if many and params is not None and not isinstance(params, (list, tuple)):
        params = list(params)  # an iterator would already be consumed; keep what we can
    span = record_query(
        str(sql),
        params,
        start=start,
        duration=duration,
        db_alias=getattr(connection, "alias", "default"),
        vendor=getattr(connection, "vendor", "other"),
        rows=rows,
        many=many,
    )
    if span is not None:
        attribute_to_view(span)
        if rows is None and not many and cursor is not None and span.data.get("explainable"):
            _count_fetched_rows(cursor, span)
        else:
            _detach_counter(cursor)
        note_sync_call(span, "sync ORM")  # sync ORM on the event loop (async views)
    else:
        _detach_counter(cursor)


class _RowCounter:
    """Counts the rows Django fetches from ``cursor`` into ``span.data["rows"]``.

    Used where ``cursor.rowcount`` is unusable (sqlite, server-side cursors): the
    ``CursorWrapper`` gets instance-level ``fetchone``/``fetchmany``/``fetchall`` that
    forward to the real ones and add what they return. ``rows`` therefore means "rows read
    so far"; a partly consumed iterator reports the rows it actually read.

    One counter per cursor (``cursor._redline_rows``), installed once with the cursor's
    true fetch methods: every later execute on the same (reused) cursor retargets it to
    the new span with a fresh count, or detaches it (``data = None``) when that execute is
    not counted, so the methods never stack up.
    """

    __slots__ = ("_all", "_many", "_one", "data", "n")

    def __init__(self, cursor: Any) -> None:
        self.data: dict[str, Any] | None = None
        self.n = 0
        self._one, self._many, self._all = cursor.fetchone, cursor.fetchmany, cursor.fetchall

    def fetchone(self) -> Any:
        row = self._one()
        if self.data is not None:
            if row is not None:
                self.n += 1
            self.data["rows"] = self.n
        return row

    def fetchmany(self, *args: Any, **kwargs: Any) -> Any:
        rows = self._many(*args, **kwargs)
        if self.data is not None:
            self.n += len(rows)
            self.data["rows"] = self.n
        return rows

    def fetchall(self) -> Any:
        rows = self._all()
        if self.data is not None:
            self.n += len(rows)
            self.data["rows"] = self.n
        return rows


def _count_fetched_rows(cursor: Any, span: Any) -> None:
    try:
        counter = cursor.__dict__.get("_redline_rows")
        if counter is None:
            counter = _RowCounter(cursor)
            cursor.fetchone, cursor.fetchmany, cursor.fetchall = (
                counter.fetchone,
                counter.fetchmany,
                counter.fetchall,
            )
            cursor._redline_rows = counter
        counter.n = 0
        counter.data = span.data
    except Exception:  # a cursor we cannot decorate just leaves rows unknown
        pass


def _detach_counter(cursor: Any) -> None:
    counter = getattr(cursor, "__dict__", _NO_DICT).get("_redline_rows")
    if counter is not None:
        counter.data = None


_NO_DICT: dict[str, Any] = {}


_wrapper = QueryRecorder()


def install_connection(connection: Any) -> None:
    """Append the query recorder to ``connection.execute_wrappers`` (once)."""
    wrappers = connection.execute_wrappers
    if not any(isinstance(w, QueryRecorder) for w in wrappers):
        wrappers.append(_wrapper)


def _on_connection_created(sender: Any, connection: Any, **kwargs: Any) -> None:
    install_connection(connection)


# ── view attribution ───────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ViewFrame:
    name: str  # dotted name, e.g. shop.views.OrderList
    file: str | None  # display path when the view is project code
    line: int
    end_line: int
    function: str


_view_cache: dict[Any, ViewFrame] = {}


def view_frame(callback: Any) -> ViewFrame:
    """Where ``callback`` (a resolved URL callback) is defined. Cached per callback."""
    try:
        return _view_cache[callback]
    except (KeyError, TypeError):
        pass
    target = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
    if target is None:
        target = inspect.unwrap(callback)
    module = getattr(target, "__module__", None) or "?"
    qualname = getattr(target, "__qualname__", None) or getattr(
        target, "__name__", type(target).__name__
    )
    name = f"{module}.{qualname}"
    file: str | None = None
    line = end = 0
    function = getattr(target, "__name__", qualname)
    try:
        if not (inspect.isclass(target) or inspect.isfunction(target)):
            target = type(target)
        path = inspect.getsourcefile(target)
        lines, line = inspect.getsourcelines(target)
        end = line + len(lines) - 1
        if path:
            from .runtime import get_runtime

            file = get_runtime().recorder.capturer.classify(path)
    except Exception:
        file = None
    frame = ViewFrame(name=name, file=file, line=line, end_line=end, function=function)
    if len(_view_cache) < 4096:
        try:
            _view_cache[callback] = frame
        except TypeError:
            pass
    return frame


def attribute_to_view(span: Any) -> None:
    """Make sure a span's callsite mentions the project view handling the request.

    Library code (a generic ``ListView``, DRF, template rendering) has no project frame,
    so the core captures no callsite; the view's definition stands in for it. When the
    project stack does not pass through the view (a serializer method called by a DRF
    viewset), the view is added as the outermost frame so the fix suggester can find the
    view's queryset.
    """
    holder = _view.get()
    vf = holder[0] if holder else None
    if vf is None or vf.file is None:
        return
    cs = span.callsite
    if cs is None:
        code = None
        try:
            from .runtime import get_runtime

            root = get_runtime().recorder.capturer
            code = linecache.getline(root._abspath(vf.file), vf.line).strip() or None
        except Exception:
            code = None
        frame = Frame(file=vf.file, line=vf.line, function=vf.function)
        span.callsite = Callsite(
            file=vf.file, line=vf.line, function=vf.function, code=code, stack=(frame,)
        )
        return
    for f in cs.stack:
        if f.file == vf.file and vf.line <= f.line <= vf.end_line:
            return
    stack = (Frame(file=vf.file, line=vf.line, function=vf.function), *cs.stack)
    span.callsite = dataclasses.replace(cs, stack=stack[-MAX_STACK:])


def instrument_view(view: Callable[..., Any], callback: Any) -> Callable[..., Any]:
    """Wrap a (possibly atomic-wrapped) view in a ``view`` span."""
    if inspect.iscoroutinefunction(view):

        @functools.wraps(view)
        async def async_view(*args: Any, **kwargs: Any) -> Any:
            if current() is None:
                return await view(*args, **kwargs)
            vf = _enter_view(callback)
            async with SpanContext(vf.name, "view", {"async": True}):
                return await view(*args, **kwargs)

        return async_view

    @functools.wraps(view)
    def sync_view(*args: Any, **kwargs: Any) -> Any:
        if current() is None:
            return view(*args, **kwargs)
        vf = _enter_view(callback)
        with SpanContext(vf.name, "view", {"async": False}):
            return view(*args, **kwargs)

    return sync_view


def _enter_view(callback: Any) -> ViewFrame:
    vf = view_frame(callback)
    holder = _view.get()
    if holder is not None:
        holder[0] = vf
    return vf


def _install_view_hook() -> None:
    original = BaseHandler.make_view_atomic
    if getattr(original, "_redline", False):
        return

    @functools.wraps(original)
    def make_view_atomic(self: BaseHandler, view: Callable[..., Any]) -> Callable[..., Any]:
        return instrument_view(original(self, view), view)

    make_view_atomic._redline = True  # type: ignore[attr-defined]
    BaseHandler.make_view_atomic = make_view_atomic  # type: ignore[method-assign]


# ── cache ───────────────────────────────────────────────────────────────────────────

_MISS = object()
_KEY_OPS = ("set", "add", "delete", "touch", "incr", "decr", "has_key", "get_or_set")
_MANY_OPS = ("set_many", "delete_many")


def _key_text(key: Any) -> str:
    text = key if isinstance(key, str) else repr(key)
    return text[:200]


def _many_keys(keys: Any) -> str:
    try:
        items = list(keys.keys()) if isinstance(keys, dict) else list(keys)
    except TypeError:
        return "?"
    shown = ", ".join(_key_text(k) for k in items[:5])
    return shown + (f" (+{len(items) - 5})" if len(items) > 5 else "")


def _cache_span(op: str, key: str, hit: bool | None, backend: str, start: float) -> None:
    record_span(
        "cache",
        f"{op} {key}"[:120],
        start,
        _perf() - start,
        {"op": op, "key": key, "hit": hit, "backend": backend},
    )


def _active() -> bool:
    return current() is not None and not _suppressed.get() and not _in_cache.get()


def _wrap_get(method: Callable[..., Any], backend: str) -> Callable[..., Any]:
    @functools.wraps(method)
    def get(key: Any, default: Any = None, version: Any = None) -> Any:
        if not _active():
            return method(key, default, version)
        token = _in_cache.set(True)
        start = _perf()
        try:
            value = method(key, _MISS, version)
        finally:
            _in_cache.reset(token)
        hit = value is not _MISS
        _cache_span("get", _key_text(key), hit, backend, start)
        return value if hit else default

    return get


def _wrap_get_many(method: Callable[..., Any], backend: str) -> Callable[..., Any]:
    @functools.wraps(method)
    def get_many(keys: Any, version: Any = None) -> Any:
        if not _active():
            return method(keys, version)
        keys = list(keys)
        token = _in_cache.set(True)
        start = _perf()
        try:
            found = method(keys, version)
        finally:
            _in_cache.reset(token)
        _cache_span("get_many", _many_keys(keys), len(found) == len(keys), backend, start)
        return found

    return get_many


def _wrap_op(op: str, method: Callable[..., Any], backend: str, many: bool):
    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _active():
            return method(*args, **kwargs)
        token = _in_cache.set(True)
        start = _perf()
        try:
            result = method(*args, **kwargs)
        finally:
            _in_cache.reset(token)
        first = args[0] if args else kwargs.get("keys", kwargs.get("data", kwargs.get("key")))
        key = _many_keys(first) if many else _key_text(first)
        hit = bool(result) if op == "has_key" else None
        _cache_span(op, key, hit, backend, start)
        return result

    return wrapper


def instrument_cache(backend: Any, alias: str) -> Any:
    """Wrap ``backend``'s public methods into ``cache`` spans (idempotent)."""
    if getattr(backend, "_redline_wrapped", False):
        return backend
    label = f"{alias} ({type(backend).__name__})"
    try:
        backend.get = _wrap_get(backend.get, label)
        backend.get_many = _wrap_get_many(backend.get_many, label)
        for op in _KEY_OPS:
            if hasattr(backend, op):
                setattr(backend, op, _wrap_op(op, getattr(backend, op), label, False))
        for op in _MANY_OPS:
            if hasattr(backend, op):
                setattr(backend, op, _wrap_op(op, getattr(backend, op), label, True))
        if hasattr(backend, "clear"):
            backend.clear = _wrap_op("clear", backend.clear, label, False)
        backend._redline_wrapped = True
    except (AttributeError, TypeError):  # exotic backend with read-only attributes
        pass
    return backend


def install_cache_handler() -> None:
    """Instrument every cache backend ``django.core.cache.caches`` creates."""
    from django.core.cache import caches

    if getattr(caches, "_redline_wrapped", False):
        return
    original = caches.create_connection

    def create_connection(alias: str) -> Any:
        return instrument_cache(original(alias), alias)

    caches.create_connection = create_connection  # type: ignore[method-assign]
    caches._redline_wrapped = True  # type: ignore[attr-defined]
    for alias in caches:
        if hasattr(caches._connections, alias):
            instrument_cache(caches[alias], alias)


# ── templates ───────────────────────────────────────────────────────────────────────


def _install_templates() -> None:
    from django.template.base import Template

    _wrap_render(Template, "render")
    try:
        from django.template.backends.jinja2 import Template as JinjaTemplate
    except Exception:  # Jinja2 not installed
        return
    _wrap_render(JinjaTemplate, "render")


def _wrap_render(cls: type, attr: str) -> None:
    original = getattr(cls, attr)
    if getattr(original, "_redline", False):
        return

    @functools.wraps(original)
    def render(self: Any, *args: Any, **kwargs: Any) -> Any:
        if current() is None:
            return original(self, *args, **kwargs)
        name = _template_name(self)
        with SpanContext(name, "template", {"template": name}):
            return original(self, *args, **kwargs)

    render._redline = True  # type: ignore[attr-defined]
    setattr(cls, attr, render)


def _template_name(template: Any) -> str:
    name = getattr(template, "name", None)
    if name:
        return str(name)
    origin = getattr(template, "origin", None)
    origin_name = getattr(origin, "template_name", None) or getattr(origin, "name", None)
    if origin_name:
        return str(origin_name)
    inner = getattr(template, "template", None)  # the Jinja2 backend wrapper
    if inner is not None and getattr(inner, "name", None):
        return str(inner.name)
    return "<string>"
