"""Additional detectors (``CONTRACT.md`` section 9).

Each module in this package exposes ``detect(trace, settings) -> list[Issue]`` and is
listed in :data:`REGISTRY`. :meth:`redline_core.detect.Detector.detect` runs the built-in
SQL detectors, then every registry entry (an exception in one is logged and skipped),
then the fix suggesters on all issues.
"""

from __future__ import annotations

from collections.abc import Callable

from ..conf import Settings
from ..model import Issue, Trace

__all__ = ["REGISTRY", "Detect"]

Detect = Callable[[Trace, Settings], list[Issue]]

#: Detector functions, in run order. Filled in by the detector modules' owners.
REGISTRY: list[Detect] = []

# Detector modules import ``redline_core.detect`` lazily (inside ``detect()``), because
# ``redline_core.detect`` imports this package.
from . import blocking, transactions  # noqa: E402

REGISTRY.append(blocking.detect)
REGISTRY.append(transactions.detect)
