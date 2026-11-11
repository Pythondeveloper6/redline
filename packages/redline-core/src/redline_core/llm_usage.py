"""Sampled LLM-call usage per route and model, from the ``llm_call`` spans of recent traces.

Tokens and counts only. Cost is left to the reader: model prices change, so redline does not
ship a price table (multiply the token counts by your own rates). Streamed calls have no token
counts, so totals undercount usage when streaming is used.
"""

from __future__ import annotations

import time
from typing import Any

from .storage.base import Filters

__all__ = ["summarize"]

MAX_TRACES = 300
_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def _tokens(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def summarize(store: Any, *, window: float = 3600.0, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    scanned = 0
    page = store.list(Filters(since=now - window), limit=MAX_TRACES)
    for summary in page.items:
        scanned += 1
        trace = store.get(summary["id"], include_profile=False)
        if trace is None:
            continue
        route = trace.route or trace.path
        for span in trace.spans:
            if span.kind != "llm_call":
                continue
            d = span.data
            key = (route, str(d.get("model") or "unknown"))
            row = rows.setdefault(
                key,
                {
                    "route": key[0],
                    "model": key[1],
                    "provider": d.get("provider"),
                    "calls": 0,
                    "errors": 0,
                    "streamed_calls": 0,
                    "total_ms": 0.0,
                    **dict.fromkeys(_FIELDS, 0),
                },
            )
            row["calls"] += 1
            row["errors"] += d.get("status") == "error"
            row["streamed_calls"] += bool(d.get("stream"))
            row["total_ms"] = round(row["total_ms"] + span.duration_ms, 3)
            for name in _FIELDS:
                row[name] += _tokens(d.get(name))
    items = sorted(
        rows.values(), key=lambda r: (-r["input_tokens"] - r["output_tokens"], r["route"])
    )
    totals: dict[str, Any] = {"calls": sum(r["calls"] for r in items)}
    for name in (*_FIELDS, "errors", "streamed_calls"):
        totals[name] = sum(r[name] for r in items)
    return {
        "sampled": True,
        "window_seconds": window,
        "traces_scanned": scanned,
        "items": items,
        "totals": totals,
        "note": "Counts come from sampled traces. Streamed calls have no token counts, so "
        "totals undercount when streaming is used. Prices are not included.",
    }
