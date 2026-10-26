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
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import parse_qsl

from .conf import Settings

__all__ = ["PROFILE_FLAG_VALUES", "Sampler", "is_error", "profile_flag", "profile_requested"]

#: Values of ``?_redline=`` and ``X-Redline-Profile`` that ask for a flamegraph
#: (case-insensitive, surrounding whitespace ignored). Both adapters use these.
PROFILE_FLAG_VALUES = frozenset(("1", "true", "yes", "on", "profile"))


def profile_flag(value: str | bytes | None) -> bool:
    """Whether one ``_redline`` / ``X-Redline-Profile`` value asks for profiling."""
    if value is None:
        return False
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    return value.strip().lower() in PROFILE_FLAG_VALUES


def profile_requested(query: Any = None, header: str | bytes | None = None) -> bool:
    """The adapters' reading of "profile this request".

    ``query`` is the raw query string (``str``/``bytes``) or a mapping of parameter values
    (a value may be a list); ``header`` is the ``X-Redline-Profile`` header value. Any
    ``_redline`` value in :data:`PROFILE_FLAG_VALUES` – ``profile``, ``1``, ``true``,
    ``yes``, ``on`` – or such a header counts.
    """
    if profile_flag(header):
        return True
    if not query:
        return False
    values: Iterable[Any]
    if isinstance(query, (str, bytes)):
        text = query.decode("latin-1") if isinstance(query, bytes) else query
        if "_redline" not in text:  # the common case costs one substring check
            return False
        values = [v for k, v in parse_qsl(text, keep_blank_values=True) if k == "_redline"]
    elif isinstance(query, Mapping):
        raw = query.get("_redline")
        values = raw if isinstance(raw, (list, tuple)) else [raw]
    else:
        return False
    return any(profile_flag(v) for v in values if isinstance(v, (str, bytes)))


def is_error(status: int, error: bool = False) -> bool:
    """Whether a request counts as an *error* for sampling and endpoint stats.

    Any 5xx is an error; so is an unhandled exception – unless the framework turned it
    into a 4xx response (Django's ``Http404``, ``PermissionDenied``…), which is a client
    error, not a failure.
    """
    if status >= 500:
        return True
    return error and not 400 <= status < 500


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
        ``X-Redline-Profile: 1`` header (:func:`profile_requested`); it only counts in
        ``opt_in``/``sampled`` modes.
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
        """The ``sampled_by`` reason to store the trace, or ``None`` to drop it.

        ``error`` is whether the request raised; see :func:`is_error` (a 4xx is never an
        error, even when raised as an exception).
        """
        s = self.settings
        if rolled:
            return "all" if s.sample_rate >= 1.0 else "rate"
        if s.always_keep_errors and is_error(status, error):
            return "error"
        if duration_ms >= s.always_keep_slower_than_ms:
            return "slow"
        if profiled:
            return "profile"
        return None
