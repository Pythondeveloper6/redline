"""Constant-cost size observations for already available binary cache values."""

import time
from contextlib import contextmanager


def value_bytes(value):
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, memoryview):
        return value.nbytes
    # Do not serialize objects or encode arbitrary strings just to measure them.
    return None


@contextmanager
def cache_decode(key: str, payload=None, *, backend: str = "custom"):
    """Explicitly measure a decode block without serializing or storing its value."""
    from .recorder import current, record_span

    builder = current()
    if builder is None or not builder.active:
        yield
        return
    safe_key = builder.recorder.redactor.cache_key(key)
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        record_span(
            "cache",
            f"decode {safe_key}",
            started,
            elapsed,
            {
                "op": "decode",
                "key": safe_key,
                "backend": backend,
                "value_bytes": value_bytes(payload),
                "decode_ms": elapsed * 1000,
            },
        )
