"""Helpers for recording calls to a client library Redline has no integration for.

:func:`instrument_client_method` wraps one method of a client class (or object) so every
call becomes a span with the client name, the operation, a target (a collection, topic,
bucket, queue), the duration, a payload size and the exception class on failure. It is the
shape the first-party pymongo, Kafka, pika and object-storage integrations share, and it
is all you need to make a database, queue or storage client visible::

    from redline_core.extending import instrument_client_method

    restore = instrument_client_method(
        acme_sdk.Client, "put",
        client="acme",
        operation="put",
        target=lambda self, args, kwargs: args[0],        # the table name
        payload_bytes=lambda self, args, kwargs: len(args[1]),
    )
    # ... restore() undoes it

:func:`client_span` is the context-manager form for code you cannot patch.

Nothing here reads or stores arguments or results: sizes are computed only from values
that are already bytes, the target is redacted and capped, and an error is recorded as its
class name, never its message.
"""

from __future__ import annotations

import functools
import inspect
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .model import SpanKind
from .recorder import current, record_span

__all__ = ["client_span", "instrument_client_method", "payload_size"]

_perf = time.perf_counter
_TARGET_CAP = 200
_MARK = "__redline_client_span__"

#: ``(self, args, kwargs) -> value`` for the per-call callbacks.
CallHook = Callable[[Any, tuple[Any, ...], dict[str, Any]], Any]


def payload_size(value: Any) -> int | None:
    """Length in bytes of a value that is already bytes-like or text, else ``None``.

    Nothing is serialized to measure it, so mappings, lists and arbitrary objects give
    ``None`` rather than a guess.
    """
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, memoryview):
        return value.nbytes
    if isinstance(value, str):
        return len(value.encode("utf-8", "replace"))
    return None


def _safe_target(target: Any) -> str | None:
    if not isinstance(target, str) or not target:
        return None
    builder = current()
    if builder is not None:
        try:
            target = builder.recorder.redactor.text(target)
        except Exception:
            return None
    return target[:_TARGET_CAP]


def _record(
    kind: SpanKind,
    client: str,
    operation: str,
    target: Any,
    start: float,
    payload: int | None,
    error: BaseException | None,
    result_size: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        safe = _safe_target(target)
        data: dict[str, Any] = {
            "client": client,
            "op": operation,
            "target": safe,
            "payload_bytes": payload,
            "error": type(error).__name__ if error is not None else None,
        }
        if result_size is not None:
            data["result_bytes"] = result_size
        if extra:
            data.update(extra)
        record_span(
            kind,
            f"{client} {operation} {safe}" if safe else f"{client} {operation}",
            start,
            _perf() - start,
            data,
        )
    except Exception:
        # Profiling must never alter the application's result or exception.
        return


def _call(hook: Any, self: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if hook is None or not callable(hook):
        return hook
    try:
        return hook(self, args, kwargs)
    except Exception:
        return None


@contextmanager
def client_span(
    client: str,
    operation: str,
    *,
    target: str | None = None,
    kind: SpanKind = "custom",
    payload_bytes: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Record the ``with`` block as one client span.

    Yields a dict; set ``info["payload_bytes"]`` (or ``info["target"]``) inside the block
    once the size is known. Outside a traced request nothing is recorded::

        with client_span("acme", "put", target="orders") as info:
            info["payload_bytes"] = len(blob)
            acme_client.put("orders", blob)
    """
    info: dict[str, Any] = {"payload_bytes": payload_bytes, "target": target}
    if current() is None:
        yield info
        return
    start = _perf()
    error: BaseException | None = None
    try:
        yield info
    except BaseException as exc:
        error = exc
        raise
    finally:
        _record(
            kind, client, operation, info.get("target"), start, info.get("payload_bytes"), error
        )


def instrument_client_method(
    owner: Any,
    method: str,
    *,
    client: str,
    operation: str | CallHook | None = None,
    target: str | CallHook | None = None,
    payload_bytes: CallHook | None = None,
    result_bytes: Callable[[Any], int | None] | None = None,
    kind: SpanKind = "custom",
) -> Callable[[], None]:
    """Wrap ``owner.method`` so each call is recorded as a span; return an undo function.

    ``owner`` is a class (every instance is covered) or one object. ``method`` may be a
    plain function or a coroutine function. Per call, the optional hooks receive
    ``(self, args, kwargs)`` and return:

    * ``operation`` - the operation name (default: the method name);
    * ``target`` - what the call acts on, such as a collection or topic (a constant string
      also works);
    * ``payload_bytes`` - the request size in bytes, e.g. ``lambda s, a, k: payload_size(a[0])``.

    ``result_bytes(result)`` gives the response size. A hook that raises is ignored: the
    call still runs and is still recorded, just without that field. Wrapping the same
    method twice is a no-op (the second undo function does nothing), and outside a traced
    request the wrapper is one ``ContextVar`` read. Raises ``AttributeError`` when the
    method does not exist.
    """
    original = getattr(owner, method)
    if getattr(original, _MARK, False):
        return lambda: None
    in_class = inspect.isclass(owner)
    # the class dict entry (so a staticmethod/plain function is restored exactly) or None
    saved = owner.__dict__.get(method) if in_class else None
    had_own = method in getattr(owner, "__dict__", {})

    def describe(self: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, ...]:
        op = _call(operation, self, args, kwargs) if callable(operation) else operation
        tgt = _call(target, self, args, kwargs) if callable(target) else target
        size = _call(payload_bytes, self, args, kwargs)
        return (
            op if isinstance(op, str) and op else method,
            tgt,
            size if isinstance(size, int) and not isinstance(size, bool) else None,
        )

    def sized(result: Any) -> int | None:
        if result_bytes is None:
            return None
        try:
            got = result_bytes(result)
        except Exception:
            return None
        return got if isinstance(got, int) and not isinstance(got, bool) else None

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if current() is None:
                return await original(*args, **kwargs)
            bound = _bound(owner, in_class, args)
            op, tgt, size = describe(bound[0], bound[1], kwargs)
            start, error, result = _perf(), None, None
            try:
                result = await original(*args, **kwargs)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                _record(
                    kind, client, op, tgt, start, size, error, sized(result) if not error else None
                )

    else:

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if current() is None:
                return original(*args, **kwargs)
            bound = _bound(owner, in_class, args)
            op, tgt, size = describe(bound[0], bound[1], kwargs)
            start, error, result = _perf(), None, None
            try:
                result = original(*args, **kwargs)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                _record(
                    kind, client, op, tgt, start, size, error, sized(result) if not error else None
                )

    setattr(wrapper, _MARK, True)
    setattr(owner, method, wrapper)

    def restore() -> None:
        target_dict = owner.__dict__ if in_class else getattr(owner, "__dict__", {})
        if target_dict.get(method) is not wrapper:
            return  # someone else replaced it after us; do not clobber their patch
        if had_own:
            setattr(owner, method, original if not in_class else saved)
        else:
            try:
                delattr(owner, method)
            except AttributeError:
                pass

    return restore


def _bound(owner: Any, in_class: bool, args: tuple[Any, ...]) -> tuple[Any, tuple[Any, ...]]:
    """``(self, args after self)``: a class wrapper receives ``self`` first, an object's
    wrapper is an instance attribute and does not."""
    if in_class and args:
        return args[0], args[1:]
    return owner, args
