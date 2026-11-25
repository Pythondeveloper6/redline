"""kafka-python ``KafkaProducer.send`` as a publish span.

Call :func:`instrument_kafka` once at startup. Each ``send`` becomes one ``custom`` span
with ``client="kafka"``, ``op="publish"``, the topic as target and the value size in bytes
when the value is already bytes. ``send`` only enqueues the record, so the span measures
the time the caller waited (metadata fetch, a full buffer), not delivery. Keys, values and
headers are never captured. kafka-python is imported only when this function runs.
"""

from __future__ import annotations

from typing import Any

from redline_core.extending import instrument_client_method, payload_size

__all__ = ["instrument_kafka", "uninstrument_kafka"]

_originals: dict[str, Any] = {}


def _topic(self: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    return args[0] if args else kwargs.get("topic")


def _value_bytes(self: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> int | None:
    return payload_size(args[1] if len(args) > 1 else kwargs.get("value"))


def instrument_kafka() -> bool:
    """Patch ``KafkaProducer.send``; ``False`` if kafka-python is missing."""
    if _originals:
        return True
    try:
        from kafka import KafkaProducer
    except ImportError:
        return False
    _originals["send"] = instrument_client_method(
        KafkaProducer,
        "send",
        client="kafka",
        operation="publish",
        target=_topic,
        payload_bytes=_value_bytes,
    )
    return True


def uninstrument_kafka() -> None:
    """Undo :func:`instrument_kafka`."""
    for restore in list(_originals.values()):
        restore()
    _originals.clear()
