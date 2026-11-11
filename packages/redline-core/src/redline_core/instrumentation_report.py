"""What redline is blind to: inactive integrations, truncated traces, dropped writes.

Everything here is derived from state redline already keeps, over a bounded sample of recent
traces. It never returns paths, URLs, SQL or payload values.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .integration_status import integration_status, integration_warnings
from .operational import health_snapshot
from .storage.base import Filters

__all__ = ["build_report"]

MAX_SAMPLE = 100


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def build_report(
    store: Any,
    runtime_health: Callable[[], dict[str, Any]] | None = None,
    *,
    window: float = 3600.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Return the instrumentation health report for the last ``window`` seconds."""
    now = time.time() if now is None else now
    health = health_snapshot(store, runtime_health)
    storage = health["storage"]
    statuses = integration_status()
    warnings = integration_warnings(statuses)

    sampled = truncated = without_profile = 0
    try:
        page = store.list(Filters(since=now - window), limit=MAX_SAMPLE)
        for summary in page.items:
            sampled += 1
            if not summary.get("has_profile"):
                without_profile += 1
            trace = store.get(summary["id"], include_profile=False)
            if trace is not None and trace.truncated:
                truncated += 1
    except Exception:  # a failing store must not take the report down
        warnings.append("Recent traces could not be read from the store.")
        page = None
    if page is not None and sampled == 0:
        warnings.append("No traces were recorded in the window; redline may not be active.")
    if truncated:
        warnings.append(
            f"{truncated} of {sampled} sampled trace(s) were truncated; counts may be undercounted."
        )
    dropped = _count(storage.get("dropped"))
    failures = _count(storage.get("write_failures"))
    if dropped or failures:
        warnings.append(
            f"{dropped} write(s) dropped and {failures} write failure(s) since the process "
            "started; recorded data is incomplete."
        )
    return {
        "window_seconds": window,
        "sampled": True,
        "traces_sampled": sampled,
        "truncated_traces": truncated,
        "without_profile": without_profile,
        "dropped_writes": dropped,
        "write_failures": failures,
        "capture_failures": _count(health["recorder"].get("capture_failures")),
        "integrations": statuses,
        "status": health["status"],
        "warnings": warnings,
    }


def configure(parser):
    parser.add_argument("--store", required=True, help="path to the recording store")
    parser.add_argument("--window", type=float, default=3600.0, help="seconds to look back")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")


def command(args, out, err):
    import json

    from .storage.multiple import TraceReader

    if not 1 <= args.window <= 30 * 86400:
        print("Use --window between 1 second and 30 days", file=err)
        return 2
    report = build_report(TraceReader(args.store), window=args.window)
    if args.json:
        out.write(json.dumps(report, indent=2) + "\n")
        return 0
    out.write(
        f"traces sampled: {report['traces_sampled']} (last {int(report['window_seconds'])}s, "
        f"sampled)\ntruncated traces: {report['truncated_traces']}\n"
        f"without profile: {report['without_profile']}\n"
        f"dropped writes: {report['dropped_writes']}\n"
    )
    for item in report["integrations"]:
        state = "active" if item["instrumented"] else ("inactive" if item["installed"] else "n/a")
        out.write(f"  {item['name']}: {state}\n")
    for warning in report["warnings"]:
        out.write(f"warning: {warning}\n")
    return 0
