"""Count model construction without storing instances or creating per-object spans."""

from __future__ import annotations

from .recorder import current


def note_instance() -> None:
    builder = current()
    if builder is not None and builder.active and builder.recorder.settings.orm_instances_threshold:
        with builder._lock:
            builder.orm_instances += 1
