"""pika ``BlockingChannel.basic_publish`` as a publish span (RabbitMQ and AMQP brokers).

Call :func:`instrument_pika` once at startup. Each publish becomes one ``custom`` span with
``client="amqp"``, ``op="publish"``, ``exchange/routing_key`` as target and the body size
in bytes. Bodies and message properties are never captured. pika is imported only when
this function runs.
"""

from __future__ import annotations

from typing import Any

from redline_core.extending import instrument_client_method, payload_size

__all__ = ["instrument_pika", "uninstrument_pika"]

_originals: dict[str, Any] = {}


def _arg(args: tuple[Any, ...], kwargs: dict[str, Any], index: int, name: str) -> Any:
    return args[index] if len(args) > index else kwargs.get(name)


def _target(self: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    exchange = _arg(args, kwargs, 0, "exchange")
    routing_key = _arg(args, kwargs, 1, "routing_key")
    return f"{exchange or '(default)'}/{routing_key or ''}"


def _body_bytes(self: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> int | None:
    return payload_size(_arg(args, kwargs, 2, "body"))


def instrument_pika() -> bool:
    """Patch ``BlockingChannel.basic_publish``; ``False`` if pika is missing."""
    if _originals:
        return True
    try:
        from pika.adapters.blocking_connection import BlockingChannel
    except ImportError:
        return False
    _originals["basic_publish"] = instrument_client_method(
        BlockingChannel,
        "basic_publish",
        client="amqp",
        operation="publish",
        target=_target,
        payload_bytes=_body_bytes,
    )
    return True


def uninstrument_pika() -> None:
    """Undo :func:`instrument_pika`."""
    for restore in list(_originals.values()):
        restore()
    _originals.clear()
