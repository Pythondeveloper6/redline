"""Typed settings shared by both adapters.

Each adapter maps its own configuration surface onto :class:`Settings`: Django reads the
``REDLINE = {...}`` dict, FastAPI takes ``install(app, settings={...})``. Both go through
:meth:`Settings.from_mapping`, which is case-insensitive on keys (``"SAMPLE_RATE"`` and
``"sample_rate"`` are the same) and rejects unknown keys with a "did you mean" hint, so a
typo never silently falls back to a default.
"""

from __future__ import annotations

import dataclasses
import difflib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "DEFAULT_IGNORE_PATHS",
    "DEFAULT_REDACT_HEADERS",
    "DEFAULT_REDACT_KEYS",
    "PROFILE_MODES",
    "ProfileMode",
    "Settings",
    "SettingsError",
]

ProfileMode = Literal["off", "opt_in", "sampled", "all"]
PROFILE_MODES: tuple[str, ...] = ("off", "opt_in", "sampled", "all")

DEFAULT_IGNORE_PATHS: tuple[str, ...] = ("/static/", "/media/", "/favicon.ico")
DEFAULT_REDACT_HEADERS: tuple[str, ...] = (
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "proxy-authorization",
)
DEFAULT_REDACT_KEYS: tuple[str, ...] = (
    r"(?i)pass(word)?|secret|token|api[_-]?key|(?<![a-z])auth(orization)?(?![a-z])"
    r"|session|csrf|card|cvv|ssn|private[_-]?key",
)


class SettingsError(ValueError):
    """A setting is unknown or has an invalid value."""


@dataclass(frozen=True)
class Settings:
    """Every Redline setting, with its default (``CONTRACT.md`` §6).

    Instances are immutable; derive a variant with :meth:`replace`.
    """

    enabled: bool = True
    storage_path: str = ".redline/redline.db"

    # sampling (what gets stored)
    sample_rate: float = 1.0
    always_keep_slower_than_ms: float = 500.0
    always_keep_errors: bool = True
    #: Path prefixes, or regular expressions when the entry starts with ``^``. Adapters
    #: append their own mount prefix so Redline never records itself.
    ignore_paths: tuple[str, ...] = DEFAULT_IGNORE_PATHS

    # capture & redaction
    capture_request_body: bool = True
    capture_response_body: bool = True
    max_body_bytes: int = 20_000
    redact_headers: tuple[str, ...] = DEFAULT_REDACT_HEADERS
    #: Regular expressions matched (``re.search``) against body/query/param key names.
    redact_keys: tuple[str, ...] = DEFAULT_REDACT_KEYS

    # detectors
    n_plus_one_threshold: int = 5
    duplicate_threshold: int = 2
    slow_query_ms: float = 100.0
    too_many_queries: int = 50

    # profiling
    profile: ProfileMode = "opt_in"
    profile_sample_rate: float = 0.0

    # explain
    explain_analyze: bool = False

    # storage & retention (``0`` or ``None`` disables a rule)
    max_rows: int | None = 10_000
    max_age_days: int | None = 7
    #: Bound of the background writer's queue; traces beyond it are dropped and counted.
    writer_queue_size: int = 1_000

    #: Spans kept per trace; beyond it spans are counted but not stored (``0``/``None``:
    #: no cap). Query counts, query time and N+1 detection stay exact.
    max_spans_per_trace: int | None = 2_000

    # callsites
    project_root: str | None = None

    def __post_init__(self) -> None:
        for name in (*_SEQ_FIELDS, *_PATH_FIELDS):
            object.__setattr__(self, name, _coerce(name, getattr(self, name)))
        _validate(self)

    # construction ----------------------------------------------------------
    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in dataclasses.fields(cls))

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None = None, **overrides: Any) -> Settings:
        """Build settings from a user-supplied dict; keys are case-insensitive.

        Raises :class:`SettingsError` for unknown keys (with a suggestion) and for values
        of the wrong type or out of range.
        """
        raw: dict[str, Any] = {}
        for key, value in {**(mapping or {}), **overrides}.items():
            if not isinstance(key, str):
                raise SettingsError(f"Redline setting names must be strings, got {key!r}.")
            raw[key.lower()] = value
        known = cls.field_names()
        unknown = sorted(set(raw) - set(known))
        if unknown:
            hints = []
            for name in unknown:
                close = difflib.get_close_matches(name, known, n=1)
                hints.append(f"{name!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
            raise SettingsError(
                "Unknown Redline setting(s): "
                + ", ".join(hints)
                + ". Valid settings: "
                + ", ".join(known)
                + "."
            )
        return cls(**{k: _coerce(k, v) for k, v in raw.items()})

    def replace(self, **changes: Any) -> Settings:
        """A copy with ``changes`` applied (validated like :meth:`from_mapping`)."""
        return Settings.from_mapping({**self.to_dict(), **changes})

    def to_dict(self) -> dict[str, Any]:
        return {name: _plain(getattr(self, name)) for name in self.field_names()}

    # derived ---------------------------------------------------------------
    @property
    def max_age_s(self) -> float | None:
        """``max_age_days`` in seconds, or ``None`` when age pruning is disabled."""
        return float(self.max_age_days) * 86_400.0 if self.max_age_days else None

    def public_dict(self) -> dict[str, Any]:
        """The subset shown by ``GET api/meta`` (no paths or patterns)."""
        return {
            "thresholds": {
                "n_plus_one": self.n_plus_one_threshold,
                "duplicate": self.duplicate_threshold,
                "slow_query_ms": self.slow_query_ms,
                "too_many_queries": self.too_many_queries,
            },
            "sampling": {
                "sample_rate": self.sample_rate,
                "always_keep_slower_than_ms": self.always_keep_slower_than_ms,
                "always_keep_errors": self.always_keep_errors,
            },
            "profiling_enabled": self.profile != "off",
            "profile_mode": self.profile,
            "explain_analyze_enabled": self.explain_analyze,
        }


_SEQ_FIELDS = {"ignore_paths", "redact_headers", "redact_keys"}
_PATH_FIELDS = {"storage_path", "project_root"}
#: Integer settings where ``0`` and ``None`` both mean "no limit".
_OPTIONAL_LIMITS = ("max_rows", "max_age_days", "max_spans_per_trace")


def _coerce(name: str, value: Any) -> Any:
    if name in _PATH_FIELDS:
        if isinstance(value, os.PathLike):
            value = os.fspath(value)
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        return value
    if name in _SEQ_FIELDS:
        if isinstance(value, str):
            value = (value,)
        try:
            return tuple(str(v) for v in value)
        except TypeError:
            raise SettingsError(f"Redline setting {name!r} must be a list of strings.") from None
    return value


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _check_type(s: Settings, name: str, types: tuple[type, ...], label: str) -> None:
    value = getattr(s, name)
    if isinstance(value, bool) and bool not in types:
        raise SettingsError(f"Redline setting {name!r} must be {label}, got {value!r}.")
    if not isinstance(value, types):
        raise SettingsError(f"Redline setting {name!r} must be {label}, got {value!r}.")


def _validate(s: Settings) -> None:
    for name in (
        "enabled",
        "always_keep_errors",
        "capture_request_body",
        "capture_response_body",
        "explain_analyze",
    ):
        _check_type(s, name, (bool,), "True or False")
    for name in (
        "max_body_bytes",
        "n_plus_one_threshold",
        "duplicate_threshold",
        "too_many_queries",
        "writer_queue_size",
    ):
        _check_type(s, name, (int,), "an integer")
        if getattr(s, name) < 0:
            raise SettingsError(f"Redline setting {name!r} must not be negative.")
    for name in _OPTIONAL_LIMITS:
        if getattr(s, name) is None:
            continue
        _check_type(s, name, (int,), "an integer (0 or None disables it)")
        if getattr(s, name) < 0:
            raise SettingsError(
                f"Redline setting {name!r} must not be negative (use 0 or None to disable it)."
            )
    for name in ("n_plus_one_threshold", "duplicate_threshold", "writer_queue_size"):
        if getattr(s, name) < 1 + (name != "writer_queue_size"):
            raise SettingsError(f"Redline setting {name!r} is too small: {getattr(s, name)}.")
    for name in (
        "sample_rate",
        "profile_sample_rate",
        "always_keep_slower_than_ms",
        "slow_query_ms",
    ):
        _check_type(s, name, (int, float), "a number")
        if getattr(s, name) < 0:
            raise SettingsError(f"Redline setting {name!r} must not be negative.")
    for name in ("sample_rate", "profile_sample_rate"):
        if not 0.0 <= getattr(s, name) <= 1.0:
            raise SettingsError(f"Redline setting {name!r} must be between 0 and 1.")
    _check_type(s, "storage_path", (str,), "a string or os.PathLike")
    if s.project_root is not None:
        _check_type(s, "project_root", (str,), "a string, os.PathLike or None")
    if s.profile not in PROFILE_MODES:
        raise SettingsError(
            f"Redline setting 'profile' must be one of {', '.join(PROFILE_MODES)}; "
            f"got {s.profile!r}."
        )
    for name in _SEQ_FIELDS:
        value = getattr(s, name)
        if not isinstance(value, tuple) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f"Redline setting {name!r} must be a list of strings.")
    for pattern in (*s.redact_keys, *(p for p in s.ignore_paths if p.startswith("^"))):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SettingsError(f"Invalid regular expression {pattern!r}: {exc}.") from None
