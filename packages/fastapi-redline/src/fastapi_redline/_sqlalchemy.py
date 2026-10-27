"""Compatibility shim: the implementation lives in :mod:`redline_core.contrib.sqlalchemy`."""

from redline_core.contrib.sqlalchemy import (
    EngineLike,
    alias_for,
    build_executor,
    instrument_engine,
    name_engines,
    vendor_of,
)

__all__ = [
    "EngineLike",
    "alias_for",
    "build_executor",
    "instrument_engine",
    "name_engines",
    "vendor_of",
]
