"""Explicitly synthetic detector evidence for exercising the development UI."""

from __future__ import annotations

import time

from . import sql as sqlmod
from .conf import Settings
from .detect import Detector
from .model import Callsite, Span, Trace, new_id

CASES = (
    "writes",
    "offset",
    "round_trips",
    "pool",
    "response",
    "aggregates",
    "models",
    "cache",
    "json",
)


def detector_sample(case: str, settings: Settings) -> Trace:
    if case not in CASES:
        raise ValueError("Unknown detector sample")
    spans = []
    site = Callsite("demo/patterns.py", 10, "synthetic_example")

    def span(kind, name, ms=20, **data):
        item = Span(
            new_id(),
            None,
            kind,
            name,
            sum(s.duration_ms for s in spans),
            ms,
            callsite=site,
            data={**data, "synthetic_demo": True},
        )
        spans.append(item)
        return item

    def query(text, ms=20):
        return span(
            "sql",
            text,
            ms,
            sql=text,
            fingerprint=sqlmod.fingerprint(text),
            params=[],
            vendor="sqlite",
            db_alias="default",
            rows=1,
            explainable=sqlmod.is_explainable(text),
            many=False,
        )

    if case == "writes":
        for _ in range(6):
            query("UPDATE products SET price = price WHERE id = 1")
    elif case == "offset":
        query("SELECT id FROM products ORDER BY id LIMIT 20 OFFSET 10000")
    elif case == "round_trips":
        for i in range(12):
            query(f"SELECT field_{i} FROM report_data", 10)
    elif case == "pool":
        span("pool_wait", "Synthetic pool checkout", 100, pool_size=5, checked_out=5)
    elif case == "aggregates":
        query("SELECT COUNT(*) FROM products WHERE price >= 0")
        query("SELECT SUM(price) FROM products WHERE price >= 0")
    elif case == "cache":
        span(
            "cache",
            "get demo:payload",
            value_bytes=2_000_000,
            decode_ms=100,
            key="demo:payload",
            op="get",
            hit=True,
            backend="synthetic",
        )
    trace = Trace(
        id=new_id(),
        framework="other",
        method="GET",
        path=f"/__demo__/{case}",
        route=f"/__demo__/{case}",
        started_at=time.time(),
        status=200,
        duration_ms=300,
        spans=spans,
        tags={"synthetic_demo": "true", "sample_case": case},
    )
    trace.query_count = sum(s.kind == "sql" for s in spans)
    trace.query_ms = sum(s.duration_ms for s in spans if s.kind == "sql")
    if case == "models":
        trace.tags["orm_instances"] = "600"
    if case == "response":
        trace.response["size"] = 2_000_000
    if case == "json":
        trace.profile = {
            "$schema": "https://www.speedscope.app/file-format-schema.json",
            "shared": {"frames": [{"name": "loads", "file": "/demo/json/__init__.py", "line": 1}]},
            "profiles": [
                {
                    "type": "evented",
                    "name": "Synthetic JSON sample",
                    "unit": "seconds",
                    "startValue": 0,
                    "endValue": 0.2,
                    "events": [
                        {"type": "O", "at": 0, "frame": 0},
                        {"type": "C", "at": 0.2, "frame": 0},
                    ],
                }
            ],
        }
    trace.issues = Detector(settings).detect(trace)
    return trace
