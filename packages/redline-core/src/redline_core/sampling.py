"""Capture policy: which requests are recorded, kept and profiled.

Recording is cheap, storing is not, so the decision is split in two:

1. **At request start** – :meth:`Sampler.ignored` drops ignored paths outright (static
   files, the Redline UI itself), the sample-rate dice are rolled once
   (:meth:`Sampler.roll`) and :meth:`Sampler.wants_profile` decides on profiling.
2. **At request end** – :meth:`Sampler.keep` stores the trace if the dice said so, *or* it
   was slow, *or* it failed, *or* it was explicitly profiled. The reason lands in
   ``trace.tags["sampled_by"]`` (``all`` / ``rate`` / ``slow`` / ``error`` / ``profile``).

With the defaults (``sample_rate=1.0``) every request is kept – the development setting.
In production lower ``sample_rate`` and rely on the slow/error safety nets.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable

from .conf import Settings

__all__ = ["Sampler"]


class Sampler:
    """Applies the sampling and profiling rules of one :class:`Settings`."""

    def __init__(self, settings: Settings | None = None, rng: Callable[[], float] = random.random):
        self.settings = settings or Settings()
        self._rng = rng
        prefixes, patterns = [], []
        for entry in self.settings.ignore_paths:
            if entry.startswith("^"):
                patterns.append(re.compile(entry))
            else:
                prefixes.append(entry)
        self._prefixes = tuple(prefixes)
        self._patterns = tuple(patterns)

    def ignored(self, path: str) -> bool:
        """Whether ``path`` must never be recorded (``ignore_paths`` or Redline disabled)."""
        if not self.settings.enabled:
            return True
        if self._prefixes and path.startswith(self._prefixes):
            return True
        return any(p.search(path) for p in self._patterns)

    def roll(self) -> bool:
        """The sample-rate dice for one request."""
        rate = self.settings.sample_rate
        return rate >= 1.0 or (rate > 0.0 and self._rng() < rate)

    def wants_profile(self, requested: bool = False) -> bool:
        """Should this request run under the profiler?

        ``requested`` is the adapter's reading of ``?_redline=profile`` or the
        ``X-Redline-Profile: 1`` header; it only counts in ``opt_in``/``sampled`` modes.
        """
        mode = self.settings.profile
        if mode == "off":
            return False
        if mode == "all":
            return True
        if requested:
            return True
        if mode == "sampled":
            rate = self.settings.profile_sample_rate
            return rate >= 1.0 or (rate > 0.0 and self._rng() < rate)
        return False

    def keep(
        self,
        *,
        rolled: bool,
        status: int,
        duration_ms: float,
        error: bool = False,
        profiled: bool = False,
    ) -> str | None:
        """The ``sampled_by`` reason to store the trace, or ``None`` to drop it."""
        s = self.settings
        if rolled:
            return "all" if s.sample_rate >= 1.0 else "rate"
        if s.always_keep_errors and (error or status >= 500):
            return "error"
        if duration_ms >= s.always_keep_slower_than_ms:
            return "slow"
        if profiled:
            return "profile"
        return None
