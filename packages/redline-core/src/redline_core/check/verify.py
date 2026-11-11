"""Did a fix remove a finding? A verdict over a saved compare report.

Absence of a finding in sampled traces is not proof it is resolved, so ``gone`` needs enough
after-fix calls and complete server-side metrics; otherwise the verdict is ``inconclusive``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["verify_report"]


def _matches(issue: Mapping[str, Any], kind: str, fingerprint: str | None) -> bool:
    return issue.get("kind") == kind and (
        fingerprint is None or issue.get("fingerprint") == fingerprint
    )


def _found(side: Any, kind: str, fingerprint: str | None) -> bool:
    issues = side.get("issues") if isinstance(side, Mapping) else None
    return any(_matches(i, kind, fingerprint) for i in issues or () if isinstance(i, Mapping))


def _speed(verdicts: list[str]) -> str:
    if "slower" in verdicts:
        return "slower"
    if "faster" in verdicts:
        return "faster"
    if verdicts and all(v == "same" for v in verdicts):
        return "unchanged"
    return "unknown"


def verify_report(
    requests: Iterable[Mapping[str, Any]],
    kind: str,
    fingerprint: str | None = None,
    *,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Verdict for one finding across the report's request entries.

    Returns ``{"kind", "fingerprint", "finding", "reason", "responses", "speed",
    "requests"}`` where ``finding`` is ``gone``, ``still_present`` or ``inconclusive``.
    """
    affected = []
    for entry in requests:
        server = entry.get("server") if isinstance(entry, Mapping) else None
        if isinstance(server, Mapping) and _found(server.get("before"), kind, fingerprint):
            affected.append(entry)
    out: dict[str, Any] = {
        "kind": kind,
        "fingerprint": fingerprint,
        "finding": "inconclusive",
        "reason": None,
        "responses": "unknown",
        "speed": "unknown",
        "requests": [e.get("name") for e in affected],
    }
    if not affected:
        out["reason"] = "the finding was not observed in the baseline, so a fix cannot be checked"
        return out
    verdicts = [str((e.get("response") or {}).get("verdict")) for e in affected]
    if all(v == "same" for v in verdicts):
        out["responses"] = "equal"
    elif any(v in ("changed", "broken", "missing") for v in verdicts):
        out["responses"] = "changed"
    out["speed"] = _speed([str((e.get("perf") or {}).get("verdict")) for e in affected])
    if any(_found(e["server"].get("after"), kind, fingerprint) for e in affected):
        out["finding"] = "still_present"
        out["reason"] = "the finding was observed again after the change"
        return out
    for e in affected:
        server, calls = e["server"], e.get("calls") or [None, None]
        after = server.get("after")
        if server.get("note") or not isinstance(after, Mapping):
            out["reason"] = "server-side metrics were unavailable after the change"
            return out
        if after.get("complete_spans") is False:
            out["reason"] = "traces after the change were truncated"
            return out
        if not isinstance(calls[1], int) or calls[1] < min_samples:
            out["reason"] = f"only {calls[1]} samples after the change; need at least {min_samples}"
            return out
    out["finding"] = "gone"
    out["reason"] = f"not observed in {len(affected)} request(s) with enough samples (sampled data)"
    return out
