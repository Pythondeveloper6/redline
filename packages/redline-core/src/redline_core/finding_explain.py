"""Plain-language reasons a finding fired: the rule, what was observed and the limits.

Built only from the evidence detectors already attach (``Issue.extra``) and the group's
occurrence count, so it never queries a database and never raises on legacy or unknown data.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["explain_issue"]

_DEFAULT_LIMITS = (
    "Based on sampled, recorded traces; unsampled requests are not counted. "
    "Cost is observed time and may overlap, so it is not a promise of savings."
)

#: kind -> (rule, grouping rule)
_RULES: dict[str, tuple[str, str]] = {
    "n_plus_one": (
        "The same SQL shape ran at least the configured number of times in one request.",
        "Same SQL fingerprint from one callsite within a single trace.",
    ),
    "duplicate_query": (
        "An identical statement with identical parameters ran more than once in a request.",
        "Same fingerprint and parameters within a single trace.",
    ),
    "slow_query": (
        "One query took longer than the slow-query threshold.",
        "Same SQL fingerprint from one callsite.",
    ),
    "too_many_queries": (
        "A request ran more queries than the per-request limit.",
        "All SQL spans in one trace.",
    ),
    "deep_offset": (
        "A query skips more rows with OFFSET than the configured limit.",
        "Same SQL fingerprint from one callsite.",
    ),
    "repeated_writes": (
        "The same INSERT or UPDATE shape ran repeatedly from one callsite in a request.",
        "Same write fingerprint from one callsite within a single trace.",
    ),
    "database_round_trips": (
        "Many individually fast queries added up to substantial cumulative query time.",
        "All SQL spans in one trace for the route; skipped when an N+1 already explains it.",
    ),
    "connection_pool_wait": (
        "A request waited for a database connection longer than the pool-wait threshold.",
        "Pool-wait spans in one trace for the route.",
    ),
    "large_response": (
        "A response was larger than the configured byte limit.",
        "One response per trace for the route.",
    ),
}

_THRESHOLD_KEYS = (
    ("threshold_ms", "{} ms"),
    ("time_threshold_ms", "{} ms"),
    ("count_threshold", "{} calls"),
    ("threshold", "{}"),
)


def _threshold(extra: Mapping[str, Any]) -> str | None:
    parts = []
    for key, fmt in _THRESHOLD_KEYS:
        if key in extra and extra[key] is not None:
            parts.append(fmt.format(extra[key]))
    return ", ".join(parts) or None


def explain_issue(kind: str, extra: Mapping[str, Any] | None, count: int) -> dict[str, Any]:
    """Return ``{rule, observed, threshold, grouping, limits, sampled}`` for one finding."""
    extra = extra if isinstance(extra, Mapping) else {}
    rule, grouping = _RULES.get(
        kind,
        (
            f"A detector for '{kind}' matched the recorded spans.",
            "Findings are grouped by their stable issue id across traces.",
        ),
    )
    observed = f"{max(int(count or 0), 0)} matching span(s) in the sampled occurrence"
    for key, label in (("offset", "OFFSET"), ("bytes", "bytes"), ("decode_ms", "decode ms")):
        if extra.get(key) is not None:
            observed += f"; {label} {extra[key]}"
    limits = _DEFAULT_LIMITS
    if extra.get("incomplete_trace"):
        limits += " This trace was incomplete (truncated), so counts may be undercounted."
    return {
        "rule": rule,
        "observed": observed,
        "threshold": _threshold(extra),
        "grouping": grouping,
        "limits": limits,
        "sampled": True,
    }
