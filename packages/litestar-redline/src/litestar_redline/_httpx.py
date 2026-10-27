"""Compatibility shim: the implementation lives in :mod:`redline_core.contrib.httpx`."""

from redline_core.contrib.httpx import instrument_httpx, uninstrument_httpx

__all__ = ["instrument_httpx", "uninstrument_httpx"]
