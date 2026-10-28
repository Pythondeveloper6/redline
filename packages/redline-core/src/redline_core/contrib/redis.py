"""redis-py commands as ``cache`` spans (sync and ``redis.asyncio``).

Call :func:`instrument_redis` once at startup. It wraps ``Redis.execute_command`` of both
clients, and ``Pipeline.execute`` (one span per pipeline, ``op="pipeline"``). Commands queued
on a pipeline are not recorded individually. Outside a traced request each wrapper is one
``ContextVar.get()``.
"""

from __future__ import annotations

import time
from typing import Any

import redis
import redis.asyncio

from redline_core import record_span
from redline_core.cache_payload import value_bytes
from redline_core.recorder import current

__all__ = ["instrument_redis", "uninstrument_redis"]

_perf = time.perf_counter
_originals: dict[str, Any] = {}
_KEY_CAP = 200
#: Commands whose result says whether the key was found (``None`` = miss).
_READS = frozenset({"get", "getex", "getdel", "hget", "mget", "hmget", "lindex"})
#: Commands whose arguments can carry credentials (``AUTH pw``, ``HELLO 3 AUTH u pw``,
#: ``CONFIG SET requirepass pw``, ``MIGRATE … AUTH pw``, ``ACL SETUSER u >pw``): no key is
#: taken from them, so nothing of theirs reaches the span name or data.
_NO_KEY = frozenset({"auth", "hello", "migrate", "config", "config set", "acl", "acl setuser"})


def _key(args: tuple[Any, ...]) -> str | None:
    """The command's key with session ids and secret-looking parts masked
    (:meth:`~redline_core.redact.Redactor.cache_key`), capped at 200 characters."""
    if len(args) < 2:
        return None
    key = args[1]
    if isinstance(key, bytes):
        key = key.decode("utf-8", "replace")
    if not isinstance(key, str):
        return None
    builder = current()
    if builder is not None:
        key = builder.recorder.redactor.cache_key(key)
    return key[:_KEY_CAP]


def _hit(op: str, result: Any) -> bool | None:
    if op not in _READS:
        return None
    if isinstance(result, (list, tuple)):
        return any(v is not None for v in result)
    return result is not None


def _record(args: tuple[Any, ...], start: float, result: Any, ok: bool) -> None:
    op = args[0]
    op = (op.decode("utf-8", "replace") if isinstance(op, bytes) else str(op)).lower()
    key = None if op in _NO_KEY else _key(args)
    size = value_bytes(result) if ok and op in _READS else None
    if size is None and op == "set" and len(args) > 2:
        size = value_bytes(args[2])
    if size is None and op in {"setex", "psetex"} and len(args) > 3:
        size = value_bytes(args[3])
    record_span(
        "cache",
        f"{op} {key}" if key else op,
        start,
        _perf() - start,
        {
            "op": op,
            "key": key,
            "hit": _hit(op, result) if ok else None,
            "backend": "redis",
            "value_bytes": size,
        },
    )


def _record_pipeline(count: int, start: float) -> None:
    record_span(
        "cache",
        f"pipeline ({count})",
        start,
        _perf() - start,
        {"op": "pipeline", "key": None, "hit": None, "backend": "redis", "commands": count},
    )


def instrument_redis() -> bool:
    """Patch redis-py (sync and asyncio). Idempotent."""
    if _originals:
        return True
    sync_exec = redis.Redis.execute_command
    async_exec = redis.asyncio.Redis.execute_command
    sync_pipe = redis.client.Pipeline.execute
    async_pipe = redis.asyncio.client.Pipeline.execute
    _originals.update(sync=sync_exec, async_=async_exec, sync_pipe=sync_pipe, async_pipe=async_pipe)

    def execute_command(self: Any, *args: Any, **options: Any) -> Any:
        if current() is None or not args:
            return sync_exec(self, *args, **options)
        start, result, ok = _perf(), None, False
        try:
            result = sync_exec(self, *args, **options)
            ok = True
            return result
        finally:
            _record(args, start, result, ok)

    async def aexecute_command(self: Any, *args: Any, **options: Any) -> Any:
        if current() is None or not args:
            return await async_exec(self, *args, **options)
        start, result, ok = _perf(), None, False
        try:
            result = await async_exec(self, *args, **options)
            ok = True
            return result
        finally:
            _record(args, start, result, ok)

    def pipe_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        if current() is None:
            return sync_pipe(self, *args, **kwargs)
        count, start = len(self.command_stack), _perf()
        try:
            return sync_pipe(self, *args, **kwargs)
        finally:
            _record_pipeline(count, start)

    async def apipe_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        if current() is None:
            return await async_pipe(self, *args, **kwargs)
        count, start = len(self.command_stack), _perf()
        try:
            return await async_pipe(self, *args, **kwargs)
        finally:
            _record_pipeline(count, start)

    redis.Redis.execute_command = execute_command  # type: ignore[method-assign]
    redis.asyncio.Redis.execute_command = aexecute_command  # type: ignore[method-assign]
    redis.client.Pipeline.execute = pipe_execute  # type: ignore[method-assign]
    redis.asyncio.client.Pipeline.execute = apipe_execute  # type: ignore[method-assign]
    return True


def uninstrument_redis() -> None:
    """Undo :func:`instrument_redis`."""
    if not _originals:
        return
    redis.Redis.execute_command = _originals.pop("sync")  # type: ignore[method-assign]
    redis.asyncio.Redis.execute_command = _originals.pop("async_")  # type: ignore[method-assign]
    redis.client.Pipeline.execute = _originals.pop("sync_pipe")  # type: ignore[method-assign]
    redis.asyncio.client.Pipeline.execute = _originals.pop("async_pipe")  # type: ignore[method-assign]
