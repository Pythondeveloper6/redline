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
import ipaddress
import math
import os
import re
import subprocess
import threading
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

__all__ = [
    "ACCESS_POLICIES",
    "DEFAULT_IGNORE_PATHS",
    "DEFAULT_REDACT_HEADERS",
    "DEFAULT_REDACT_KEYS",
    "EDITORS",
    "FEATURES",
    "LOG_LEVELS",
    "MIN_TOKEN_LENGTH",
    "PRESETS",
    "PROFILE_MODES",
    "SECRET_SETTINGS",
    "ProfileMode",
    "Settings",
    "SettingsError",
    "editor_url_template",
    "resolve_release",
]

ProfileMode = Literal["off", "opt_in", "sampled", "all"]
PROFILE_MODES: tuple[str, ...] = ("off", "opt_in", "sampled", "all")

LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
#: Editor names accepted by ``Settings.editor`` and the URL template each one opens.
EDITORS: dict[str, str | None] = {
    "vscode": "vscode://file/{file}:{line}",
    "cursor": "cursor://file/{file}:{line}",
    "pycharm": "pycharm://open?file={file}&line={line}",
    "idea": "idea://open?file={file}&line={line}",
    "sublime": "subl://open?url=file://{file}&line={line}",
    "none": None,
}

#: ``Settings.access`` policies (``CONTRACT.md`` §18).
ACCESS_POLICIES: tuple[str, ...] = ("local", "token", "basic", "framework", "custom", "open")
#: Switches of ``Settings.features``; a missing key means on.
FEATURES: tuple[str, ...] = (
    "explain",
    "profile",
    "replay",
    "replay_writes",
    "snapshot",
    "load",
    "logs",
    "locals",
    "sse",
    "checks",
)
#: What each preset changes; a preset only sets defaults – any explicit value wins.
PRESETS: dict[str, dict[str, Any]] = {
    "dev": {},
    "shared-dev": {
        "capture_request_body": False,
        "capture_response_body": False,
        "capture_locals": False,
        "capture_error_details": False,
        "capture_logs": False,
        "capture_sql_params": False,
        "log_level": "WARNING",
        "access": "token",
        "read_only": True,
        "max_age_days": 3,
        "load_from_ui": False,
        "checks_from_ui": False,
        "analysis_from_ui": False,
        "features": {"replay": False, "replay_writes": False},
    },
    "staging": {
        "sample_rate": 0.25,
        "access": "token",
        "capture_locals": False,
        "capture_sql_params": False,
        "features": {"replay_writes": False},
    },
    "production": {
        "writer_queue_size": 64,
        "max_trace_bytes": 262_144,
        "max_span_data_bytes": 8_192,
        "max_sql_bytes": 16_384,
        "max_spans_per_trace": 500,
        "capture_sql_params": False,
        "capture_error_details": False,
        "capture_logs": False,
        "features": {
            "replay": False,
            "replay_writes": False,
            "load": False,
            "checks": False,
            "explain": False,
            "profile": False,
        },
        "sample_rate": 0.05,
        "always_keep_errors": True,
        "capture_request_body": False,
        "capture_response_body": False,
        "capture_locals": False,
        "log_level": "WARNING",
        "access": "token",
        "read_only": True,
        "badge": False,
        "server_timing": False,
        "load_from_ui": False,
        "checks_from_ui": False,
        "analysis_from_ui": False,
        "explain_analyze": False,
        "profile": "off",
    },
}
#: Settings that take ``"auto"`` besides True/False.
_AUTO_FIELDS: tuple[str, ...] = (
    "badge",
    "server_timing",
    "checks_from_ui",
    "analysis_from_ui",
    "cookie_secure",
)
#: Settings never shown in clear (``GET api/config``, ``redline config``).
SECRET_SETTINGS: tuple[str, ...] = ("token", "secret_key", "basic_users")
#: Prefix of the hashes made by ``redline hash-password``.
PASSWORD_HASH_PREFIX = "pbkdf2_sha256$"
#: Minimum token length outside the ``dev`` preset.
MIN_TOKEN_LENGTH = 24

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
    #: A ``postgresql://`` URL: store traces in that shared database instead of the SQLite
    #: file at ``storage_path`` (needs ``redline-core[postgres]``).
    storage_url: str | None = None
    storage_role: str = "web"
    extra_stores: tuple[str, ...] = ()

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

    capture_sql_params: bool | str = True
    redact_profiles: tuple[str, ...] = ()
    redact_tables: tuple[str, ...] = ()
    redact_routes: Mapping[str, str] = field(default_factory=dict)
    capture_loggers: tuple[str, ...] = ()
    ignore_loggers: tuple[str, ...] = ()
    session_expected: Mapping[str, str] = field(default_factory=lambda: {"NOCOUNT": "OFF"})
    session_expected_context: tuple[str, ...] = ()
    user_resolver: Any = None
    request_id_headers: tuple[str, ...] = ("X-Request-ID", "X-Correlation-ID", "traceparent")
    allow_storage_in_repo: bool = False
    instrument_engines: str = "explicit"

    # detectors
    n_plus_one_threshold: int = 5
    duplicate_threshold: int = 2
    sql_retry_storm_threshold: int = 3
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
    max_sql_bytes: int = 65_536
    max_span_data_bytes: int = 32_768
    max_tag_bytes: int = 2_048
    max_trace_bytes: int = 1_048_576

    #: Spans kept per trace; beyond it spans are counted but not stored (``0``/``None``:
    #: no cap). Query counts, query time and N+1 detection stay exact.
    max_spans_per_trace: int | None = 2_000

    # callsites
    project_root: str | None = None

    # v0.2 detectors (CONTRACT.md section 10)
    blocking_ms: float = 20.0
    detect_blocking: bool = True
    large_result_rows: int = 1_000
    unbounded_rows: int = 200
    slow_serializer_ms: float = 50.0
    long_transaction_ms: float = 200.0
    slow_external_ms: float = 500.0

    orm_instances_threshold: int = 500
    repeated_writes_threshold: int = 5
    deep_offset_rows: int = 10000
    round_trip_count: int = 10
    repeated_scans_threshold: int = 2
    large_response_bytes: int = 1000000
    large_cache_bytes: int = 1000000
    round_trip_ms: float = 100.0
    pool_wait_ms: float = 50.0
    json_decode_ms: float = 50.0
    cache_decode_ms: float = 50.0

    # logs & exceptions
    capture_logs: bool = True
    #: Minimum level captured into a trace (case-insensitive; stored upper-case).
    log_level: str = "INFO"
    max_logs_per_trace: int = 200
    capture_locals: bool = True
    capture_error_details: bool = True

    # developer experience
    #: ``vscode``, ``cursor``, ``pycharm``, ``idea``, ``sublime``, ``none`` or a template
    #: containing ``{file}`` (absolute path) and ``{line}``.
    editor: str = "vscode"
    #: ``Server-Timing`` header on recorded responses (trace id, query count/time – a side
    #: channel for anyone who can make requests); ``"auto"`` = in framework debug mode.
    server_timing: bool | str = "auto"
    #: Inject the floating badge into HTML responses; ``"auto"`` = when the framework is
    #: in debug mode.
    badge: bool | str = "auto"
    #: ``None`` resolves automatically, see :func:`resolve_release`.
    release: str | None = None

    # history & load testing
    history_retention_days: int | None = 90
    load_from_ui: bool = False
    #: Start refactor-check runs (baseline / compare) from the UI; ``"auto"`` = only while
    #: the framework is in debug mode (``CONTRACT.md`` §19.5).
    checks_from_ui: bool | str = "auto"
    analysis_from_ui: bool | str = "auto"

    # configuration & access control (CONTRACT.md section 18)
    #: ``dev`` / ``staging`` / ``production``: a set of defaults, see :data:`PRESETS`. It
    #: is applied by :func:`redline_core.config.resolve_settings`, not by this class.
    preset: str = "dev"
    #: Who may open the UI/API: one of :data:`ACCESS_POLICIES`.
    access: str = "local"
    #: ``access="custom"``: ``callable(RequestInfo) -> bool`` or its dotted path.
    access_check: Any = None
    #: IPs/CIDRs. ``local``: allowed besides loopback; other policies: when non-empty, the
    #: client must also be in one of them.
    allowed_ips: tuple[str, ...] = ()
    #: Extra ``Host`` names the ``local`` policy's DNS-rebinding check accepts.
    allowed_hosts: tuple[str, ...] = ()
    #: Peers (IPs/CIDRs) whose ``X-Forwarded-For/-Proto/-Host`` headers are believed.
    trusted_proxies: tuple[str, ...] = ()
    token: str | None = None
    token_file: str | None = None
    #: ``{username: hash}`` for ``access="basic"`` (hashes from ``redline hash-password``).
    basic_users: Mapping[str, str] = field(default_factory=dict)
    #: Signs the login cookie. Unset: a random per-process key (single-process servers).
    secret_key: str | None = None
    read_only: bool = False
    #: Feature switches, see :data:`FEATURES` (missing keys are on).
    features: Mapping[str, bool] = field(default_factory=dict)
    ui_enabled: bool = True
    #: Where adapters mount the UI/API when their own argument is not given (Django: the
    #: URLconf decides).
    mount_path: str | None = None
    #: Target of load runs started from the UI. Unset: the app's own URL as the request
    #: addressed it, accepted only for a loopback host or one in ``allowed_hosts``.
    base_url: str | None = None
    session_ttl_s: int = 43_200
    #: Failed logins allowed per client IP per 5 minutes.
    max_login_attempts: int = 10
    #: ``access="local"``: count a unix-socket peer (no client address) as local. Off by
    #: default: a reverse proxy on this machine talking to the app over a unix socket would
    #: otherwise make every visitor "local".
    trust_unix_socket: bool = False
    #: ``Secure`` attribute of the login cookie: ``"auto"`` = when the request's scheme is
    #: https (``X-Forwarded-Proto`` counts only from ``trusted_proxies``); True/False force it.
    cookie_secure: bool | str = "auto"

    def __post_init__(self) -> None:
        for name in (
            *_SEQ_FIELDS,
            *_PATH_FIELDS,
            *_MAP_FIELDS,
            "log_level",
            "badge",
            "server_timing",
            "checks_from_ui",
            "analysis_from_ui",
            "cookie_secure",
            "preset",
            "access",
            "mount_path",
        ):
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
        redact = raw.pop("redact", None)
        if redact is not None:
            if not isinstance(redact, Mapping) or set(redact) - {
                "keys",
                "tables",
                "profiles",
                "routes",
            }:
                raise SettingsError("redact accepts keys, tables, profiles and routes")
            for name, value in redact.items():
                target = "redact_" + name
                if target in raw:
                    raise SettingsError(f"Configure either redact.{name} or {target}, not both")
                raw[target] = value
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

    def feature(self, name: str) -> bool:
        """Whether feature ``name`` (see :data:`FEATURES`) is switched on."""
        return bool(self.features.get(name, True))

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


_SEQ_FIELDS = {
    "extra_stores",
    "redact_profiles",
    "redact_tables",
    "capture_loggers",
    "ignore_loggers",
    "request_id_headers",
    "session_expected_context",
    "ignore_paths",
    "redact_headers",
    "redact_keys",
    "allowed_ips",
    "allowed_hosts",
    "trusted_proxies",
}
_PATH_FIELDS = {"storage_path", "project_root", "token_file"}
_MAP_FIELDS = {"basic_users", "features", "redact_routes", "session_expected"}
#: Integer settings where ``0`` and ``None`` both mean "no limit".
_OPTIONAL_LIMITS = ("max_rows", "max_age_days", "max_spans_per_trace", "history_retention_days")


def _coerce(name: str, value: Any) -> Any:
    if name in _PATH_FIELDS:
        if isinstance(value, os.PathLike):
            value = os.fspath(value)
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        return value
    if name == "log_level" and isinstance(value, str):
        return value.strip().upper()
    if name in ("preset", "access") and isinstance(value, str):
        return value.strip().lower()
    if name == "mount_path" and isinstance(value, str):
        return "/" + value.strip().strip("/")
    if name in _MAP_FIELDS:
        return _coerce_mapping(name, value)
    if name in _AUTO_FIELDS and isinstance(value, str):
        return value.strip().lower() if value.strip().lower() == "auto" else value
    if name in _SEQ_FIELDS:
        if isinstance(value, str):
            value = (value,)
        try:
            return tuple(str(v) for v in value)
        except TypeError:
            raise SettingsError(f"Redline setting {name!r} must be a list of strings.") from None
    return value


def _coerce_mapping(name: str, value: Any) -> Mapping[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise SettingsError(f"Redline setting {name!r} must be a mapping (a dict or TOML table).")
    if name == "features":
        items = {(k.strip().lower() if isinstance(k, str) else k): v for k, v in value.items()}
        items = {**dict.fromkeys(FEATURES, True), **items}
    else:
        items = dict(value)
    return types.MappingProxyType(items)  # read-only: settings are immutable


def _plain(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, types.MappingProxyType):
        return dict(value)
    return value


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
        "detect_blocking",
        "capture_logs",
        "capture_locals",
        "capture_error_details",
        "load_from_ui",
        "read_only",
        "ui_enabled",
        "trust_unix_socket",
        "allow_storage_in_repo",
    ):
        _check_type(s, name, (bool,), "True or False")
    for name in (
        "max_sql_bytes",
        "max_span_data_bytes",
        "max_tag_bytes",
        "max_trace_bytes",
        "orm_instances_threshold",
        "repeated_writes_threshold",
        "deep_offset_rows",
        "round_trip_count",
        "repeated_scans_threshold",
        "large_response_bytes",
        "large_cache_bytes",
        "max_body_bytes",
        "n_plus_one_threshold",
        "duplicate_threshold",
        "sql_retry_storm_threshold",
        "too_many_queries",
        "writer_queue_size",
        "large_result_rows",
        "unbounded_rows",
        "max_logs_per_trace",
        "session_ttl_s",
        "max_login_attempts",
    ):
        _check_type(s, name, (int,), "an integer")
        if getattr(s, name) < 0:
            raise SettingsError(f"Redline setting {name!r} must not be negative.")
    for name, minimum in {
        "max_sql_bytes": 256,
        "max_span_data_bytes": 256,
        "max_tag_bytes": 32,
        "max_trace_bytes": 4096,
    }.items():
        if getattr(s, name) < minimum:
            raise SettingsError(f"{name} must be at least {minimum}")
    for name in _OPTIONAL_LIMITS:
        if getattr(s, name) is None:
            continue
        _check_type(s, name, (int,), "an integer (0 or None disables it)")
        if getattr(s, name) < 0:
            raise SettingsError(
                f"Redline setting {name!r} must not be negative (use 0 or None to disable it)."
            )
    for name in (
        "n_plus_one_threshold",
        "duplicate_threshold",
        "sql_retry_storm_threshold",
        "writer_queue_size",
        "session_ttl_s",
        "max_login_attempts",
    ):
        if getattr(s, name) < (2 if name.endswith("threshold") else 1):
            raise SettingsError(f"Redline setting {name!r} is too small: {getattr(s, name)}.")
    for name in (
        "sample_rate",
        "profile_sample_rate",
        "always_keep_slower_than_ms",
        "round_trip_ms",
        "pool_wait_ms",
        "json_decode_ms",
        "cache_decode_ms",
        "slow_query_ms",
        "blocking_ms",
        "slow_serializer_ms",
        "long_transaction_ms",
        "slow_external_ms",
    ):
        _check_type(s, name, (int, float), "a number")
        if getattr(s, name) < 0:
            raise SettingsError(f"Redline setting {name!r} must not be negative.")
    for name in ("round_trip_ms", "pool_wait_ms", "json_decode_ms", "cache_decode_ms"):
        value = getattr(s, name)
        if isinstance(value, float) and not math.isfinite(value):
            raise SettingsError(f"Redline setting {name!r} must be finite.")
    for name in ("sample_rate", "profile_sample_rate"):
        if not 0.0 <= getattr(s, name) <= 1.0:
            raise SettingsError(f"Redline setting {name!r} must be between 0 and 1.")
    _check_type(s, "storage_path", (str,), "a string or os.PathLike")
    if s.storage_url is not None:
        _check_type(s, "storage_url", (str,), "a postgresql:// URL or None")
        if not s.storage_url.strip().lower().startswith(("postgresql://", "postgres://")):
            raise SettingsError(
                "Redline setting 'storage_url' must be a postgresql:// URL "
                "(the SQLite file is configured with storage_path)."
            )
    if s.project_root is not None:
        _check_type(s, "project_root", (str,), "a string, os.PathLike or None")
    if s.profile not in PROFILE_MODES:
        raise SettingsError(
            f"Redline setting 'profile' must be one of {', '.join(PROFILE_MODES)}; "
            f"got {s.profile!r}."
        )
    if s.log_level not in LOG_LEVELS:
        raise SettingsError(
            f"Redline setting 'log_level' must be one of {', '.join(LOG_LEVELS)} "
            f"(case-insensitive); got {s.log_level!r}."
        )
    for name in _AUTO_FIELDS:
        value = getattr(s, name)
        if not isinstance(value, bool) and value != "auto":
            raise SettingsError(
                f"Redline setting {name!r} must be 'auto', True or False; got {value!r}."
            )
    _check_type(s, "editor", (str,), "an editor name or a template")
    if s.editor not in EDITORS and not ("{file}" in s.editor and "{line}" in s.editor):
        raise SettingsError(
            f"Redline setting 'editor' must be one of {', '.join(EDITORS)} or a template "
            f"containing {{file}} and {{line}}; got {s.editor!r}."
        )
    if s.release is not None:
        _check_type(s, "release", (str,), "a string or None")
    for name in _SEQ_FIELDS:
        value = getattr(s, name)
        if not isinstance(value, tuple) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f"Redline setting {name!r} must be a list of strings.")
    for pattern in (*s.redact_keys, *(p for p in s.ignore_paths if p.startswith("^"))):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SettingsError(f"Invalid regular expression {pattern!r}: {exc}.") from None
    if not isinstance(s.capture_sql_params, bool) and s.capture_sql_params != "reads_only":
        raise SettingsError("capture_sql_params must be True, False or 'reads_only'")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in s.session_expected.items()):
        raise SettingsError("session_expected maps option names to expected SQL values")
    if s.user_resolver is not None and not callable(s.user_resolver):
        raise SettingsError("user_resolver must be a synchronous callable or None")
    if not isinstance(s.storage_role, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", s.storage_role):
        raise SettingsError("storage_role must be a simple role name")
    if len(s.extra_stores) > 8:
        raise SettingsError("At most eight extra_stores are supported")
    if s.instrument_engines not in {"explicit", "all", "flask_sqlalchemy"}:
        raise SettingsError("instrument_engines must be explicit, all or flask_sqlalchemy")
    from .privacy import PROFILES

    if set(s.redact_profiles) - set(PROFILES):
        raise SettingsError("Unknown redaction profile")
    if any(
        not isinstance(k, str) or v not in {"body", "values"} for k, v in s.redact_routes.items()
    ):
        raise SettingsError("redact_routes maps method/path patterns to body or values")
    for pattern in s.redact_tables:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SettingsError(f"Invalid table pattern: {exc}") from None
    _validate_access(s)


_GENERATE_TOKEN = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'


def _validate_access(s: Settings) -> None:
    """The section-18 settings, including the cross-field rules of the access policy."""
    if s.preset not in PRESETS:
        raise SettingsError(
            f"Redline setting 'preset' must be one of {', '.join(PRESETS)}; got {s.preset!r}."
        )
    if s.access not in ACCESS_POLICIES:
        raise SettingsError(
            f"Redline setting 'access' must be one of {', '.join(ACCESS_POLICIES)}; "
            f"got {s.access!r}."
        )
    for name in ("allowed_ips", "trusted_proxies"):
        for entry in getattr(s, name):
            try:
                ipaddress.ip_network(entry.strip(), strict=False)
            except ValueError:
                raise SettingsError(
                    f"Redline setting {name!r}: {entry!r} is not an IP address or CIDR network "
                    "(e.g. '10.0.0.0/8', '192.168.1.7', 'fd00::/8')."
                ) from None
    if s.base_url is not None:
        _check_type(s, "base_url", (str,), "an http(s) URL or None")
        parts = urlsplit(s.base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.query:
            raise SettingsError(
                "Redline setting 'base_url' must be an http(s) URL such as "
                f"'http://127.0.0.1:8000'; got {s.base_url!r}."
            )
    for name in ("token", "token_file", "secret_key", "mount_path"):
        if getattr(s, name) is not None:
            _check_type(s, name, (str,), "a string or None")
    if s.token is not None:
        if not s.token or any(c.isspace() or not c.isprintable() for c in s.token):
            raise SettingsError(
                "Redline setting 'token' must be a non-empty string without whitespace."
            )
        if s.token_file is not None:
            raise SettingsError("Set either the Redline 'token' or 'token_file', not both.")
    if s.secret_key is not None and len(s.secret_key) < 16:
        raise SettingsError("Redline setting 'secret_key' must be at least 16 characters long.")
    if s.mount_path is not None and (
        s.mount_path == "/" or any(c in s.mount_path for c in ';,"\\ \r\n\t')
    ):
        raise SettingsError(
            f"Redline setting 'mount_path' must be a sub-path such as '/_redline'; "
            f"got {s.mount_path!r}."
        )
    for feature, on in s.features.items():
        if feature not in FEATURES:
            close = difflib.get_close_matches(str(feature), FEATURES, n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            raise SettingsError(
                f"Unknown Redline feature {feature!r}{hint}. Features: {', '.join(FEATURES)}."
            )
        if not isinstance(on, bool):
            raise SettingsError(f"Redline feature {feature!r} must be True or False; got {on!r}.")
    for user, hashed in s.basic_users.items():
        if not isinstance(user, str) or not user or ":" in user:
            raise SettingsError(
                "Redline setting 'basic_users': user names must be non-empty strings without "
                f"':'; got {user!r}."
            )
        if not isinstance(hashed, str) or not hashed.startswith(PASSWORD_HASH_PREFIX):
            raise SettingsError(
                f"Redline setting 'basic_users': the password of {user!r} must be a hash made "
                "by `redline hash-password` (it starts with 'pbkdf2_sha256$'), never the "
                "plain password."
            )
    if s.access_check is not None and not (
        callable(s.access_check) or isinstance(s.access_check, str)
    ):
        raise SettingsError(
            "Redline setting 'access_check' must be a callable(request_info) -> bool or its "
            "dotted path."
        )
    if s.access == "token" and s.token is None and s.token_file is None:
        why = f" (the {s.preset!r} preset uses access='token')" if s.preset != "dev" else ""
        raise SettingsError(
            f"access='token' needs a token{why}: set 'token' (e.g. the REDLINE_TOKEN "
            f"environment variable) or 'token_file'. Generate one with: {_GENERATE_TOKEN}"
        )
    if s.access == "token" and s.token is not None and s.preset != "dev":
        check_token_strength(s.token, s.preset)
    if s.access == "basic" and not s.basic_users:
        raise SettingsError(
            "access='basic' needs at least one user in 'basic_users' "
            "({username: hash}; make hashes with `redline hash-password`)."
        )
    if s.access == "custom" and s.access_check is None:
        raise SettingsError(
            "access='custom' needs 'access_check': a callable(request_info) -> bool "
            "(or its dotted path)."
        )


def check_token_strength(token: str, preset: str) -> None:
    """Raise :class:`SettingsError` when ``token`` is too short for ``preset``."""
    if preset != "dev" and len(token) < MIN_TOKEN_LENGTH:
        raise SettingsError(
            f"The Redline token must be at least {MIN_TOKEN_LENGTH} characters long with the "
            f"{preset!r} preset (it has {len(token)}). Generate one with: {_GENERATE_TOKEN}"
        )


# ── v0.2 helpers ─────────────────────────────────────────────────────────────────────

_RELEASE_ENV = (
    "REDLINE_RELEASE",
    "GIT_SHA",
    "SOURCE_VERSION",
    "RENDER_GIT_COMMIT",
    "HEROKU_SLUG_COMMIT",
)
_release_cache: dict[str, str | None] = {}
_release_lock = threading.Lock()


def _git_short_sha(cwd: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except Exception:  # no git, bad cwd, timeout...
        return None
    out = proc.stdout.strip() if proc.returncode == 0 else ""
    return out or None


def resolve_release(settings: Settings) -> str | None:
    """The release label for ``settings`` (``Settings.release`` or the automatic rule).

    Order: the explicit setting, env ``REDLINE_RELEASE``, then ``GIT_SHA`` /
    ``SOURCE_VERSION`` / ``RENDER_GIT_COMMIT`` / ``HEROKU_SLUG_COMMIT``, then
    ``git rev-parse --short HEAD`` in ``project_root`` (1 s timeout). The git lookup is
    cached per process and per directory. Never raises.
    """
    try:
        if settings.release:
            return settings.release
        for name in _RELEASE_ENV:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        root = os.path.abspath(settings.project_root or os.getcwd())
        with _release_lock:
            if root in _release_cache:
                return _release_cache[root]
        sha = _git_short_sha(root)
        with _release_lock:
            _release_cache[root] = sha
        return sha
    except Exception:  # pragma: no cover - defensive
        return None


def editor_url_template(settings: Settings) -> str | None:
    """The ``{file}``/``{line}`` URL template for ``settings.editor`` (``None`` = disabled)."""
    editor = settings.editor
    if editor in EDITORS:
        return EDITORS[editor]
    return editor
