"""Queue-independent job recording. Arguments and row payloads are never captured."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Protocol

from .capture_limits import bounded_value
from .recorder import current, get_recorder, span


class TaskQueueIntegration(Protocol):
    def uninstall(self) -> None: ...


def _label(value, builder=None):
    budget = builder.recorder.settings.max_tag_bytes if builder else 2048
    safe, cut = bounded_value(
        value, max_bytes=budget, max_string_bytes=min(1024, budget), max_items=20
    )
    if builder is not None:
        builder.capture_limited |= cut
    text = safe if type(safe) is str else json.dumps(safe, ensure_ascii=True, separators=(",", ":"))
    return text


def tag(key, value):
    """Tag the current recording; no-op outside recording. Values are bounded and redacted."""
    builder = current()
    if builder is not None and builder.active and not builder.finished:
        if type(key) is not str or len(key) > 128:
            builder.capture_limited = True
            return
        redactor = builder.recorder.redactor
        text = _label(value, builder)
        builder.tag(key, "[redacted]" if redactor.is_secret_key(key) else redactor.text(text))


@contextmanager
def job(name, *, recorder=None, task_id=None, queue=None, attempt=0, parent=None):
    """Record one job, preserving an outer request/job context on exit."""
    rec = recorder or get_recorder()
    label = rec.redactor.text(_label(name))
    with rec.recording(framework="other", method="TASK", path=label, route=label) as builder:
        tag("job.name", name)
        if task_id is not None:
            tag("job.id", task_id)
        if queue is not None:
            tag("job.queue", queue)
        tag("job.attempt", attempt)
        if type(parent) is dict:
            for key in ("request_id", "parent_trace_id"):
                if parent.get(key) is not None:
                    tag(key, parent[key])
        elif parent is not None:
            builder.capture_limited = True
        try:
            yield builder
        except BaseException:
            tag("job.outcome", "failed")
            raise
        else:
            if "job.outcome" not in builder.tags:
                tag("job.outcome", "succeeded")


@contextmanager
def stage(name):
    """A child span naming the current processing stage and its observed outcome.

    The first stage that raises also tags the job with ``job.first_failing_stage`` so the
    jobs summary can name it without reading every retained span tree.
    """
    builder = current()
    label = _label(name, builder)
    if builder is not None:
        label = builder.recorder.redactor.text(label)
    with span(label, kind="custom", job_stage=True) as context:
        try:
            yield context
        except BaseException:
            context.data["stage_outcome"] = "failed"
            if builder is not None and builder.active and not builder.finished:
                if "job.first_failing_stage" not in builder.tags:
                    tag("job.first_failing_stage", label)
            raise
        else:
            context.data.setdefault("stage_outcome", "succeeded")


def row_outcome(status, reason="unspecified", **ids):
    """Count bounded private categories; never store row data or supplied identifiers."""
    if type(status) is not str or status not in {
        "failed",
        "parked",
        "conflict",
        "skipped",
        "succeeded",
    }:
        raise ValueError("Unknown row outcome status")
    builder = current()
    if builder is None or not builder.active or builder.finished:
        return
    # Hash only a bounded normalized reason, never arbitrary application conversion.
    reason = _label(reason, builder)
    key = f"{status}:{hashlib.sha256(reason.encode()).hexdigest()[:16]}"
    with builder._lock:
        counts = builder.job_outcomes
        if key not in counts and len(counts) >= 100:
            key = f"{status}:other"
            builder.capture_limited = True
        value = counts.get(key, 0) + 1
        counts[key] = min(value, 2**63 - 1)
        builder.capture_limited |= value > 2**63 - 1
        # Tags are presentation only. Never read possibly clipped or user-supplied JSON
        # back into counters. Collapse categories until valid JSON fits the tag budget.
        shown = dict(counts)
        budget = builder.recorder.settings.max_tag_bytes

        def encode(values):
            return json.dumps(values, sort_keys=True, separators=(",", ":"))

        text = encode(shown)
        if len(json.dumps(text, ensure_ascii=True)) > budget:
            builder.capture_limited = True
            shown = {}
            for category, count in counts.items():
                group = category.split(":", 1)[0] + ":other"
                shown[group] = min(shown.get(group, 0) + count, 2**63 - 1)
            text = encode(shown)
            while shown and len(json.dumps(text, ensure_ascii=True)) > budget:
                shown.pop(next(reversed(shown)))
                text = encode(shown)
        builder.tag("job.row_outcomes", text)


def propagation_headers():
    builder = current()
    if builder is None or not builder.active or builder.finished:
        return {}
    return {
        "parent_trace_id": builder.id,
        **({"request_id": builder.tags["request_id"]} if builder.tags.get("request_id") else {}),
    }
