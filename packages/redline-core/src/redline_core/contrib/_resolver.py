"""Shared machinery for the GraphQL integrations: one ``resolver`` span per field.

A span is opened for every resolution but **kept only when it is worth showing**: it had
children (SQL, cache, a nested kept resolver, ...), it took at least
:data:`MIN_KEPT_MS`, or it raised. That drops the thousands of trivial attribute
resolvers a large response produces while keeping every resolver that touched the
database. It uses the recorder's context variables directly (same package, so these are
internal-but-stable) because :func:`redline_core.recorder.span` cannot discard a span.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from .. import recorder as _rec
from ..model import Span, new_id

#: A childless resolver faster than this is dropped.
MIN_KEPT_MS = 0.5


def field_path(info: Any) -> str:
    """``orders.*.customer`` for ``info.path`` (list indexes become ``*``)."""
    keys: list[str] = []
    node = getattr(info, "path", None)
    while node is not None:
        key = node.key
        keys.append("*" if isinstance(key, int) else str(key))
        node = node.prev
    return ".".join(reversed(keys))


class _Open:
    __slots__ = ("builder", "data", "id", "n_dropped", "n_queries", "n_spans", "prev", "start")

    def __init__(self, builder: _rec.TraceBuilder, data: dict[str, Any]) -> None:
        self.builder = builder
        self.data = data
        self.id = new_id()
        self.prev: str | None = _rec._parent.get()
        self.n_spans = len(builder.spans)
        self.n_dropped = builder.dropped_spans
        self.n_queries = builder.query_count
        self.start = _rec._perf()


def _finish(state: _Open, exc: BaseException | None) -> None:
    builder = state.builder
    if builder.finished:
        return
    ms = (_rec._perf() - state.start) * 1000.0
    data = state.data
    if exc is not None:
        data["error"] = type(exc).__name__
    elif ms < MIN_KEPT_MS:
        sid = state.id
        childless = (
            builder.dropped_spans == state.n_dropped
            and builder.query_count == state.n_queries
            and not any(s.parent_id == sid for s in builder.spans[state.n_spans :])
        )
        if childless:
            return
    builder.add(
        Span(
            id=state.id,
            parent_id=state.prev,
            kind="resolver",
            name=f"{data['parent_type']}.{data['field']}",
            start_ms=round(max(0.0, (state.start - builder.t0) * 1000.0), 3),
            duration_ms=round(ms, 3),
            callsite=None,
            data=data,
        )
    )


def _attach(state: _Open) -> Any:
    return _rec._parent.set(state.id)


def _detach(token: Any, state: _Open) -> None:
    try:
        _rec._parent.reset(token)
    except ValueError:  # reset from another context (async task hop)
        _rec._parent.set(state.prev)


def trace_resolver(
    next_: Callable[..., Any], root: Any, info: Any, args: tuple, kwargs: dict
) -> Any:
    """Call ``next_(root, info, *args, **kwargs)`` inside a resolver span.

    Awaitable results (coroutines, futures) and promise-like results (``.then``) are
    wrapped so the span covers the asynchronous part too, with the span as the parent of
    anything recorded while it runs.
    """
    builder = _rec._trace.get()
    if builder is None or builder.finished:
        return next_(root, info, *args, **kwargs)
    parent_type = getattr(info, "parent_type", None)
    data = {
        "path": field_path(info),
        "field": getattr(info, "field_name", ""),
        "parent_type": getattr(parent_type, "name", None) or str(parent_type),
    }
    state = _Open(builder, data)
    token = _attach(state)
    try:
        result = next_(root, info, *args, **kwargs)
    except BaseException as exc:
        _detach(token, state)
        _finish(state, exc)
        raise
    _detach(token, state)
    if inspect.isawaitable(result):
        return _await(result, state)
    if callable(getattr(result, "then", None)):  # promise (graphene 2 style)
        return result.then(
            lambda value: (_finish(state, None), value)[1],
            lambda err: (_finish(state, err), _raise(err))[1],
        )
    _finish(state, None)
    return result


def _raise(err: BaseException) -> None:
    raise err


async def _await(awaitable: Any, state: _Open) -> Any:
    token = _attach(state)
    try:
        value = await awaitable
    except BaseException as exc:
        _detach(token, state)
        _finish(state, exc)
        raise
    _detach(token, state)
    _finish(state, None)
    return value
