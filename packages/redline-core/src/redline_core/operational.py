"""Profiler health without SQL queries, provider imports, paths or payload values."""

from __future__ import annotations

_STORAGE_FIELDS = {
    "status",
    "scope",
    "pid",
    "background",
    "closed",
    "queue_depth",
    "queue_capacity",
    "writer_started",
    "writer_alive",
    "written",
    "dropped",
    "write_failures",
    "flush_failures",
    "last_write_error_type",
    "last_flush_succeeded",
    "shutdown_complete",
    "shutdown_failures",
}
_RUNTIME_FIELDS = {"status", "scope", "pid", "enabled", "capture_failures"}
_DROP_REASONS = {
    "closed",
    "queue_full",
    "write_failure",
    "lifecycle_write_failure",
    "enqueue_failure",
    "shutdown_timeout",
    "writer_unavailable",
}


def _snapshot(provider, fields):
    if not callable(provider):
        return {"status": "unknown", "available": False}
    try:
        value = provider()
        if type(value) is not dict:
            raise TypeError
        result = {
            key: val
            for key, val in value.items()
            if key in fields and type(val) in (str, bool, int, type(None))
        }
        if result.get("status") not in {"ok", "degraded", "closed", "disabled"}:
            result["status"] = "unknown"
        if fields is _STORAGE_FIELDS:
            reasons = value.get("dropped_by_reason")
            if type(reasons) is dict:
                result["dropped_by_reason"] = {
                    key: count
                    for key, count in reasons.items()
                    if key in _DROP_REASONS and type(count) is int and count >= 0
                }
        return {**result, "available": True}
    except Exception:
        return {"status": "unknown", "available": False}


def health_snapshot(store, runtime_health=None):
    try:
        provider = getattr(store, "operational_health", None)
    except Exception:
        provider = None
    storage = _snapshot(provider, _STORAGE_FIELDS)
    runtime = _snapshot(runtime_health, _RUNTIME_FIELDS)
    status = "ok" if storage["status"] == runtime["status"] == "ok" else "degraded"
    return {
        "status": status,
        "storage": storage,
        "recorder": runtime,
        "note": "Profiler health for this process only; not application readiness "
        "or a fleet aggregate. "
        "Counters are cumulative since process initialization; flush completion does not "
        "prove persistence when drops/write failures are reported.",
    }
