"""redline-core: the framework-agnostic engine of Redline.

Redline is a self-hosted request profiler and backend analyzer for Django and FastAPI.
This package holds everything that does not depend on a web framework – the data model,
the ``contextvars`` recorder, SQL fingerprinting, callsite capture, redaction, the N+1 /
duplicate / slow-query detectors, EXPLAIN plan analysis, the optional pyinstrument
profiler, the SQLite store and the JSON API. It has no runtime dependencies.

Applications install ``django-redline`` or ``fastapi-redline``; the only thing most user
code touches directly is :func:`span`::

    import redline_core

    @redline_core.span("pricing.compute")
    def compute_price(order): ...

    async with redline_core.span("fetch-rates", provider="ecb"):
        ...
"""

from __future__ import annotations

__version__ = "0.4.0a2"

from .conf import Settings, SettingsError
from .detect import Detector, FixSuggester, GenericSuggester
from .jobs import job, row_outcome, stage, tag
from .model import Callsite, Frame, Issue, Span, Suggestion, Trace
from .recorder import (
    Recorder,
    SpanContext,
    TraceBuilder,
    configure,
    current,
    finish_trace,
    get_recorder,
    record_query,
    record_span,
    span,
    start_trace,
)

__all__ = [
    "Callsite",
    "Detector",
    "FixSuggester",
    "Frame",
    "GenericSuggester",
    "Issue",
    "Recorder",
    "Settings",
    "SettingsError",
    "Span",
    "SpanContext",
    "Suggestion",
    "Trace",
    "TraceBuilder",
    "__version__",
    "configure",
    "current",
    "finish_trace",
    "get_recorder",
    "job",
    "record_query",
    "record_span",
    "row_outcome",
    "span",
    "stage",
    "start_trace",
    "tag",
]
