"""Prometheus text exposition (format 0.0.4) of the recorder's health counters.

No ``prometheus_client`` dependency: the counters are rendered by hand. Served at
``<mount>/api/metrics`` behind the same access rules as every other API endpoint. Labels
are fixed names (never request data), so the output cannot leak recorded content.
"""

from __future__ import annotations

from typing import Any

__all__ = ["render_metrics"]


def _num(value: Any) -> str:
    return repr(float(value)) if isinstance(value, float) else str(int(value))


def render_metrics(store: Any, recorder: Any = None) -> str:
    """Counters and gauges for ``store`` (and ``recorder`` when it writes to that store)."""
    health = getattr(store, "health", None)
    data: dict[str, Any] = health() if callable(health) else {}
    dropped = dict(data.get("dropped") or {})
    if not dropped and getattr(store, "dropped", 0):
        dropped = {"other": int(store.dropped)}  # a store that cannot say why
    capture_failures = int(getattr(recorder, "dropped_capture", 0)) if recorder else 0

    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[str, Any]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(f"{name}{labels} {_num(value)}" for labels, value in samples)

    metric(
        "redline_writer_queue_depth",
        "gauge",
        "Recordings waiting for the background writer.",
        [("", data.get("queue_depth", 0))],
    )
    metric(
        "redline_writer_queue_capacity",
        "gauge",
        "Bound of the writer queue (writer_queue_size).",
        [("", data.get("queue_capacity", 0))],
    )
    metric(
        "redline_traces_saved_total",
        "counter",
        "Recordings written to the store since the process started.",
        [("", data.get("written", getattr(store, "written", 0)))],
    )
    metric(
        "redline_traces_dropped_total",
        "counter",
        "Recordings that were not stored, by reason.",
        [
            *[(f'{{reason="{reason}"}}', dropped[reason]) for reason in sorted(dropped)],
            ('{reason="capture_failed"}', capture_failures),
        ],
    )
    metric(
        "redline_write_failures_total",
        "counter",
        "Store write batches that failed.",
        [("", data.get("write_failures", 0))],
    )
    return "\n".join(lines) + "\n"
