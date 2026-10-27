"""Opt-in outbound ``requests`` spans; the implementation is :mod:`redline_core.contrib.requests`.

``requests`` is not a dependency of flask-redline, so it is imported on first use.
"""

from __future__ import annotations

__all__ = ["instrument_requests", "uninstrument_requests"]


def instrument_requests() -> bool:
    """Patch ``requests`` so every call becomes an ``http_out`` span. Idempotent."""
    from redline_core.contrib.requests import instrument_requests as instrument

    return instrument()


def uninstrument_requests() -> None:
    """Undo :func:`instrument_requests`."""
    from redline_core.contrib.requests import uninstrument_requests as uninstrument

    uninstrument()
