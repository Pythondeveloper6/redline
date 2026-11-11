"""Rank findings by observed (sampled) cost. Never sums overlapping costs into savings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["rank_by_impact"]


def rank_by_impact(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``items`` ordered by observed ms then occurrences, suppressed ones last.

    Each item gains ``impact``: sampled observed time and counts. ``additive`` is always
    ``False`` because concurrent or nested spans overlap, so costs must not be summed.
    """
    out = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        row["impact"] = {
            "observed_ms": float(item.get("observed_total_ms") or 0.0),
            "occurrences": int(item.get("occurrences") or 0),
            "routes": len(item.get("routes") or ()),
            "basis": "sampled",
            "additive": False,
            "suppressed": bool(item.get("suppressed")),
        }
        out.append(row)
    out.sort(
        key=lambda r: (
            r["impact"]["suppressed"],
            -r["impact"]["observed_ms"],
            -r["impact"]["occurrences"],
        )
    )
    return out
