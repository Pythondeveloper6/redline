"""Layered configuration (``CONTRACT.md`` §18).

Every adapter builds its :class:`~redline_core.conf.Settings` through
:func:`resolve_settings`, which merges, later layers winning:

1. ``default``   – the :class:`Settings` field defaults;
2. ``adapter``   – the adapter's own defaults (Django: ``storage_path``/``project_root`` from
   ``BASE_DIR``, ``sample_rate`` 0.1 without ``DEBUG``);
3. ``preset``    – :data:`~redline_core.conf.PRESETS` for the selected ``preset``;
4. ``file``      – ``REDLINE_CONFIG=<path>``, else ``redline.toml``, else ``[tool.redline]`` in
   ``pyproject.toml`` – looked up in ``project_root`` (default: the working directory);
5. ``env``       – ``REDLINE_<SETTING>`` environment variables, parsed by the setting's type;
6. ``framework`` – what the application passed (``REDLINE = {...}``, ``app.config["REDLINE"]``,
   ``install(settings=...)``, ``RedlinePlugin(settings=...)``).

The result is a :class:`ResolvedConfig`: the settings, where each value came from, and
warnings about risky combinations. Unknown keys in the file or the environment raise
:class:`~redline_core.conf.SettingsError` with a "did you mean" hint, so a typo never
silently falls back to a default.

A native access hook passed to an adapter (Django's ``PERMISSION``/staff check, FastAPI
``dependencies=``, Flask ``permission=``, Litestar ``guards=``) implies
``access="framework"`` unless ``access`` is set explicitly (file, env or framework layer –
a preset does not count).
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .conf import (
    FEATURES,
    MIN_TOKEN_LENGTH,
    PRESETS,
    SECRET_SETTINGS,
    Settings,
    SettingsError,
)

__all__ = [
    "CONFIG_ENV",
    "ENV_PREFIX",
    "GROUPS",
    "IGNORED_ENV",
    "SOURCES",
    "ResolvedConfig",
    "config_warnings",
    "find_config_file",
    "load_config_file",
    "mask_secrets",
    "mask_url_userinfo",
    "parse_env",
    "parse_env_value",
    "resolve_settings",
]

ENV_PREFIX = "REDLINE_"
#: Path of a config file that replaces the ``project_root`` lookup.
CONFIG_ENV = "REDLINE_CONFIG"
#: ``REDLINE_*`` variables that are not settings (never reported as unknown).
IGNORED_ENV = frozenset({"REDLINE_CONFIG", "REDLINE_ALLOW_OPEN", "REDLINE_RELEASE"})
_IGNORED_ENV_PREFIXES = ("REDLINE_TEST_",)
_ENV_NAME = re.compile(r"^REDLINE_[A-Z][A-Z0-9_]*$")

SOURCES = ("default", "adapter", "preset", "file", "env", "framework")

#: How ``GET api/config`` and ``redline config`` group the settings.
GROUPS: dict[str, tuple[str, ...]] = {
    "capture": (
        "enabled",
        "capture_sql_params",
        "redact_profiles",
        "redact_tables",
        "redact_routes",
        "capture_loggers",
        "ignore_loggers",
        "ignore_sql",
        "request_id_headers",
        "capture_request_body",
        "capture_response_body",
        "max_body_bytes",
        "redact_headers",
        "redact_keys",
        "ignore_paths",
        "capture_logs",
        "log_level",
        "max_logs_per_trace",
        "capture_locals",
        "capture_error_details",
        "max_spans_per_trace",
        "max_sql_bytes",
        "max_span_data_bytes",
        "max_tag_bytes",
        "max_trace_bytes",
        "profile",
        "profile_sample_rate",
        "server_timing",
        "badge",
    ),
    "sampling": ("sample_rate", "always_keep_slower_than_ms", "always_keep_errors"),
    "detectors": (
        "n_plus_one_threshold",
        "duplicate_threshold",
        "slow_query_ms",
        "too_many_queries",
        "blocking_ms",
        "detect_blocking",
        "large_result_rows",
        "unbounded_rows",
        "slow_serializer_ms",
        "long_transaction_ms",
        "slow_external_ms",
        "repeated_writes_threshold",
        "deep_offset_rows",
        "round_trip_count",
        "round_trip_ms",
        "repeated_scans_threshold",
        "large_response_bytes",
        "large_cache_bytes",
        "pool_wait_ms",
        "orm_instances_threshold",
        "json_decode_ms",
        "cache_decode_ms",
        "explain_analyze",
    ),
    "storage": (
        "storage_path",
        "max_rows",
        "max_age_days",
        "history_retention_days",
        "writer_queue_size",
    ),
    "access": (
        "preset",
        "access",
        "access_check",
        "allowed_ips",
        "allowed_hosts",
        "trusted_proxies",
        "trust_unix_socket",
        "token",
        "token_file",
        "basic_users",
        "secret_key",
        "read_only",
        "session_ttl_s",
        "max_login_attempts",
        "cookie_secure",
        "ui_enabled",
        "mount_path",
    ),
    "features": ("features", "load_from_ui", "checks_from_ui", "analysis_from_ui", "base_url"),
}


@dataclass(frozen=True)
class ResolvedConfig:
    """The effective settings plus where each value came from."""

    settings: Settings
    #: ``{setting: source}``, source being one of :data:`SOURCES`.
    sources: Mapping[str, str]
    #: The config file that was read, if any.
    file: str | None = None
    warnings: tuple[str, ...] = ()
    #: The ``REDLINE_*`` environment variable behind each ``env`` value.
    env_names: Mapping[str, str] = field(default_factory=dict)

    @property
    def access_explicit(self) -> bool:
        """Whether ``access`` was set by the user (file, env or framework layer). A
        default/adapter/preset ``local`` is served in framework debug mode only."""
        return self.sources.get("access", "default") not in ("default", "adapter", "preset")

    def public(self, extra_warnings: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
        """``GET api/config``: secrets masked as ``"***"``, provenance and warnings."""
        values = mask_secrets(self.settings.to_dict())
        values = {k: _jsonable(v) for k, v in values.items()}
        groups: dict[str, list[str]] = {}
        seen: set[str] = set()
        for group, names in GROUPS.items():
            groups[group] = [n for n in names if n in values]
            seen.update(groups[group])
        groups["other"] = [n for n in values if n not in seen]
        warnings = list(dict.fromkeys([*self.warnings, *extra_warnings]))
        return {
            "preset": self.settings.preset,
            "file": self.file,
            "settings": values,
            "sources": dict(self.sources),
            "groups": groups,
            "warnings": warnings,
        }


def mask_secrets(values: Mapping[str, Any]) -> dict[str, Any]:
    """``values`` with every secret replaced by ``"***"`` (``None`` stays ``None``)."""
    out = dict(values)
    for name in SECRET_SETTINGS:
        if name not in out:
            continue
        value = out[name]
        if name == "basic_users" and isinstance(value, Mapping):
            out[name] = dict.fromkeys(value, "***")
        elif value is not None:
            out[name] = "***"
    if isinstance(out.get("base_url"), str):
        out["base_url"] = mask_url_userinfo(out["base_url"])
    return out


def mask_url_userinfo(url: str) -> str:
    """``http://user:pw@host/x`` → ``http://***@host/x`` (unchanged without userinfo)."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    end = len(rest)
    for ch in "/?#":
        i = rest.find(ch)
        if i != -1:
            end = min(end, i)
    netloc = rest[:end]
    if "@" not in netloc:
        return url
    return f"{scheme}://***@{netloc.rsplit('@', 1)[1]}{rest[end:]}"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if callable(value):
        module = getattr(value, "__module__", None)
        name = getattr(value, "__qualname__", None) or type(value).__name__
        return f"{module}.{name}" if module else name
    return str(value)


# ── types (for environment parsing) ───────────────────────────────────────────────────

_FIELD_TYPES: dict[str, str] = {f.name: str(f.type) for f in dataclasses.fields(Settings)}


def _kind(name: str) -> str:
    t = _FIELD_TYPES[name].replace(" ", "")
    if name in ("badge", "server_timing", "checks_from_ui", "analysis_from_ui", "cookie_secure"):
        return "badge"
    if t.startswith("tuple["):
        return "list"
    if t.startswith("Mapping[") or t.startswith("dict["):
        return "dict"
    optional = t.endswith("|None")
    base = t.removesuffix("|None")
    if base in ("bool", "int", "float"):
        return base + ("?" if optional else "")
    return "str?" if optional else "str"


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_NONE = {"", "none", "null"}


def _parse_bool(text: str, label: str) -> bool:
    low = text.strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise SettingsError(
        f"{label} must be a boolean (1/0, true/false, yes/no, on/off); got {text!r}."
    )


def parse_env_value(name: str, raw: str, *, label: str | None = None) -> Any:
    """Parse the text of ``REDLINE_<NAME>`` for setting ``name`` by its type.

    bool: ``1/0 true/false yes/no on/off``; int/float; ``""``/``none``/``null`` → ``None``
    for optional settings; lists: a JSON array or comma-separated values; dicts: a JSON
    object or ``key=value`` pairs separated by commas (feature values are booleans).
    """
    label = label or f"{ENV_PREFIX}{name.upper()}"
    kind = _kind(name)
    text = raw.strip()
    if name == "capture_sql_params":
        return "reads_only" if text == "reads_only" else _parse_bool(text, label)
    if kind.endswith("?") and text.lower() in _NONE:
        return None
    base = kind.rstrip("?")
    try:
        if base == "bool":
            return _parse_bool(text, label)
        if base == "int":
            return int(text)
        if base == "float":
            return float(text)
    except ValueError:
        raise SettingsError(
            f"{label} must be {'an integer' if base == 'int' else 'a number'}; got {raw!r}."
        ) from None
    if base == "badge":
        return "auto" if text.lower() == "auto" else _parse_bool(text, label)
    if base == "list":
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError as exc:
                raise SettingsError(f"{label} is not a valid JSON array: {exc}.") from None
            if not isinstance(value, list):
                raise SettingsError(f"{label} must be a JSON array or comma-separated values.")
            return [str(v) for v in value]
        return [part.strip() for part in text.split(",") if part.strip()]
    if base == "dict":
        if text.startswith("{"):
            try:
                value = json.loads(text)
            except ValueError as exc:
                raise SettingsError(f"{label} is not a valid JSON object: {exc}.") from None
            if not isinstance(value, dict):
                raise SettingsError(f"{label} must be a JSON object or key=value pairs.")
        else:
            value = {}
            for part in text.split(","):
                if not part.strip():
                    continue
                key, sep, val = part.partition("=")
                if not sep:
                    raise SettingsError(f"{label}: expected key=value, got {part.strip()!r}.")
                value[key.strip()] = val.strip()
        if name == "features":
            value = {
                k: v if isinstance(v, bool) else _parse_bool(str(v), f"{label} ({k})")
                for k, v in value.items()
            }
        return value
    return raw if name in ("token", "secret_key") else text


def _env_candidates(environ: Mapping[str, str]) -> dict[str, str]:
    out = {}
    for key, value in environ.items():
        if not _ENV_NAME.match(key) or key in IGNORED_ENV:
            continue
        if key.startswith(_IGNORED_ENV_PREFIXES):
            continue
        out[key] = value
    return out


def parse_env(environ: Mapping[str, str] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """``REDLINE_*`` variables → ``({setting: value}, {setting: variable name})``.

    Raises :class:`SettingsError` for a variable that names no setting (with a hint).
    """
    environ = os.environ if environ is None else environ
    known = Settings.field_names()
    values: dict[str, Any] = {}
    names: dict[str, str] = {}
    unknown: list[str] = []
    for key, raw in sorted(_env_candidates(environ).items()):
        name = key[len(ENV_PREFIX) :].lower()
        if name not in known:
            unknown.append(key)
            continue
        if name == "access_check" and not raw.strip():
            continue
        values[name] = parse_env_value(name, raw, label=key)
        names[name] = key
    if unknown:
        env_known = [f"{ENV_PREFIX}{n.upper()}" for n in known]
        hints = []
        for key in unknown:
            close = difflib.get_close_matches(key, env_known, n=1)
            hints.append(key + (f" (did you mean {close[0]}?)" if close else ""))
        raise SettingsError(
            "Unknown Redline environment variable(s): "
            + ", ".join(hints)
            + f". Every {ENV_PREFIX}<NAME> variable must name a setting (except "
            + ", ".join(sorted(IGNORED_ENV))
            + " and REDLINE_TEST_*)."
        )
    return values, names


# ── config file ───────────────────────────────────────────────────────────────────────


def _toml_loads(text: str) -> dict[str, Any] | None:
    """Parse TOML, or ``None`` when no parser is available (3.10 without ``tomli``)."""
    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib.loads(text)
    try:  # pragma: no cover - Python 3.10 only
        import tomli
    except ImportError:  # pragma: no cover
        return None
    return tomli.loads(text)  # pragma: no cover


def find_config_file(
    project_root: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """``REDLINE_CONFIG``, else ``redline.toml``, else a ``pyproject.toml`` that has a
    ``[tool.redline]`` table, in ``project_root`` (default: the working directory)."""
    environ = os.environ if environ is None else environ
    explicit = environ.get(CONFIG_ENV, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SettingsError(f"{CONFIG_ENV}={explicit!r}: no such file.")
        return path
    root = Path(project_root) if project_root else Path.cwd()
    candidate = root / "redline.toml"
    if candidate.is_file():
        return candidate
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            return None
        if re.search(r"^\s*\[\s*tool\s*\.\s*redline\s*[\].]", text, re.MULTILINE):
            return pyproject
    return None


def load_config_file(path: str | os.PathLike[str]) -> tuple[dict[str, Any] | None, list[str]]:
    """The settings table of ``path`` (``redline.toml``: the whole file, optionally under
    ``[redline]``/``[tool.redline]``; ``pyproject.toml``: ``[tool.redline]``).

    Returns ``(None, [warning])`` when TOML cannot be parsed on this Python (3.10 without
    ``tomli``). Raises :class:`SettingsError` for invalid TOML.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsError(f"Cannot read the Redline config file {str(p)!r}: {exc}.") from None
    try:
        data = _toml_loads(text)
    except ValueError as exc:  # tomllib.TOMLDecodeError subclasses ValueError
        raise SettingsError(f"Invalid TOML in {str(p)!r}: {exc}.") from None
    if data is None:  # pragma: no cover - Python 3.10 without tomli
        return None, [
            f"The Redline config file {str(p)!r} was ignored: reading TOML needs Python 3.11+ "
            "or the 'tomli' package."
        ]
    tool = data.get("tool")
    if p.name == "pyproject.toml":
        table = tool.get("redline", {}) if isinstance(tool, dict) else {}
    elif isinstance(tool, dict) and isinstance(tool.get("redline"), dict):
        table = tool["redline"]
    elif isinstance(data.get("redline"), dict) and len(data) == 1:
        table = data["redline"]
    else:
        table = data
    if not isinstance(table, dict):
        raise SettingsError(f"The Redline table in {str(p)!r} must be a table.")
    return dict(table), []


# ── resolution ────────────────────────────────────────────────────────────────────────


def _lower_keys(mapping: Mapping[str, Any] | None, where: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (mapping or {}).items():
        if not isinstance(key, str):
            raise SettingsError(f"Redline setting names must be strings, got {key!r} ({where}).")
        out[key.lower()] = value
    redact = out.pop("redact", None)
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
            if target in out:
                raise SettingsError(f"Configure either redact.{name} or {target}, not both")
            out[target] = value
    return out


def _check_known(values: Mapping[str, Any], where: str) -> None:
    known = Settings.field_names()
    unknown = sorted(set(values) - set(known))
    if not unknown:
        return
    hints = []
    for name in unknown:
        close = difflib.get_close_matches(name, known, n=1)
        hints.append(f"{name!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
    raise SettingsError(f"Unknown Redline setting(s) in {where}: " + ", ".join(hints) + ".")


def _explicit_from_settings(settings: Settings) -> dict[str, Any]:
    """A ``Settings`` instance as a framework layer: the fields that differ from defaults."""
    default = Settings().to_dict()
    return {k: v for k, v in settings.to_dict().items() if default.get(k) != v}


def resolve_settings(
    framework: Mapping[str, Any] | Settings | None = None,
    *,
    adapter_defaults: Mapping[str, Any] | None = None,
    native_hook: bool = False,
    environ: Mapping[str, str] | None = None,
    project_root: str | os.PathLike[str] | None = None,
    read_file: bool = True,
) -> ResolvedConfig:
    """Merge every layer into :class:`Settings` (see the module docstring).

    ``framework``
        The application's own settings: a mapping (case-insensitive keys) or a
        ``Settings`` instance (then only the fields that differ from the defaults count
        as explicit).
    ``adapter_defaults``
        The adapter's defaults, below the preset.
    ``native_hook``
        The adapter has a native access hook (Django's permission, FastAPI
        ``dependencies``, Flask ``permission``, Litestar ``guards``): ``access`` becomes
        ``"framework"`` unless set explicitly (file, env or framework layer).
    ``environ`` / ``project_root``
        For tests and the CLI; default ``os.environ`` / the resolved ``project_root``.
    """
    environ = os.environ if environ is None else environ
    if isinstance(framework, Settings):
        fw = _explicit_from_settings(framework)
    else:
        fw = _lower_keys(framework, "the framework settings")
    adapter = _lower_keys(adapter_defaults, "the adapter defaults")
    _check_known(adapter, "the adapter defaults")
    env, env_names = parse_env(environ)

    # The project root decides where the config file is looked up.
    root = fw.get("project_root") or env.get("project_root") or project_root
    root = root or adapter.get("project_root")
    file_values: dict[str, Any] = {}
    file_path: Path | None = None
    warnings: list[str] = []
    if read_file:
        file_path = find_config_file(os.fspath(root) if root else None, environ)
        if file_path is not None:
            loaded, notes = load_config_file(file_path)
            warnings.extend(notes)
            if loaded is None:
                file_path = None
            else:
                file_values = _lower_keys(loaded, str(file_path))
                _check_known(file_values, str(file_path))

    layers: list[tuple[str, dict[str, Any]]] = [
        ("adapter", adapter),
        ("file", file_values),
        ("env", env),
        ("framework", fw),
    ]
    preset = "dev"
    for _, values in layers:
        if "preset" in values and values["preset"] is not None:
            preset = str(values["preset"]).strip().lower()
    if preset not in PRESETS:
        raise SettingsError(
            f"Redline setting 'preset' must be one of {', '.join(PRESETS)}; got {preset!r}."
        )
    layers.insert(1, ("preset", dict(PRESETS[preset])))

    merged: dict[str, Any] = {}
    sources = dict.fromkeys(Settings.field_names(), "default")
    for source, values in layers:
        for key, value in values.items():
            if (
                key == "features"
                and isinstance(value, Mapping)
                and isinstance(merged.get("features"), Mapping)
            ):
                value = {**merged["features"], **value}  # feature switches merge per key
            merged[key] = value
            sources[key] = source
    merged["preset"] = preset
    if native_hook and sources["access"] in ("default", "adapter", "preset"):
        merged["access"] = "framework"
        sources["access"] = "adapter"

    try:
        settings = Settings.from_mapping(merged)
    except SettingsError as exc:
        raise SettingsError(_with_source(str(exc), sources, env_names, file_path)) from None
    warnings.extend(config_warnings(settings, sources))
    return ResolvedConfig(
        settings=settings,
        sources=sources,
        file=str(file_path) if file_path else None,
        warnings=tuple(dict.fromkeys(warnings)),
        env_names=env_names,
    )


def _with_source(
    message: str, sources: Mapping[str, str], env_names: Mapping[str, str], file: Path | None
) -> str:
    """Append where the offending value came from to a validation message."""
    for name in Settings.field_names():
        if f"'{name}'" in message:
            source = sources.get(name, "default")
            if source == "env":
                return f"{message} (from the environment variable {env_names.get(name)})"
            if source == "file" and file is not None:
                return f"{message} (from {file})"
            if source != "default":
                return f"{message} (from the {source} settings)"
            break
    return message


def config_warnings(settings: Settings, sources: Mapping[str, str] | None = None) -> list[str]:
    """Risky-but-valid combinations, shown by ``GET api/config`` and ``redline config``."""
    sources = sources or {}
    s = settings
    out: list[str] = []
    if s.access == "open":
        out.append(
            "access='open': anyone who can reach the app can read every captured request "
            "(headers, bodies, SQL). It is refused unless REDLINE_ALLOW_OPEN=1 is set."
        )
    if s.access == "local" and sources.get("access") in ("default", "adapter", "preset"):
        out.append(
            "access is not set: the UI and API answer 404 unless the framework is in debug "
            "mode (then the 'local' default applies). Set access explicitly – 'token' or "
            "'basic' for a server, or 'local' to accept the same-host reverse-proxy risk."
        )
    if s.access == "local" and s.trust_unix_socket:
        out.append(
            "trust_unix_socket is on: a reverse proxy on this machine that reaches the app "
            "over a unix socket makes every visitor local."
        )
    if s.cookie_secure is False and s.access in ("token", "basic"):
        out.append("cookie_secure is False: the login cookie is sent over plain http as well.")
    if s.access == "local" and s.preset != "dev":
        out.append(
            f"access='local' with the {s.preset!r} preset: only loopback clients get in "
            "(and requests forwarded by an untrusted proxy are refused). Use access='token' "
            "or 'basic' to reach Redline on a server."
        )
    if s.access == "local" and any(_is_wide(n) for n in s.allowed_ips):
        out.append(
            "allowed_ips includes a public or very wide network: every client in it can open "
            "Redline without credentials."
        )
    if s.access == "token" and s.token is not None and len(s.token) < MIN_TOKEN_LENGTH:
        out.append(
            f"The Redline token is short ({len(s.token)} characters); use at least "
            f"{MIN_TOKEN_LENGTH} (refused outside the dev preset)."
        )
    if s.access in ("token", "basic") and s.secret_key is None:
        out.append(
            "secret_key is not set: login cookies are signed with a random per-process key, "
            "so with several worker processes a UI login only works on the worker that "
            "issued it (and every restart logs everyone out). Set secret_key (e.g. "
            "REDLINE_SECRET_KEY) for multi-worker deployments."
        )
    if s.access in ("token", "basic") and not s.trusted_proxies and s.preset != "dev":
        out.append(
            "trusted_proxies is empty: behind a reverse proxy every client has the proxy's "
            "address, so the login rate limit and allowed_ips apply to all clients together."
        )
    if s.preset == "production":
        for name in ("capture_request_body", "capture_response_body", "capture_locals"):
            if getattr(s, name) and sources.get(name) not in (None, "default", "preset"):
                out.append(
                    f"{name} is on with the 'production' preset: captured data may include "
                    "personal data or secrets that redaction does not recognise."
                )
    if s.server_timing is True and s.preset != "dev":
        out.append(
            "server_timing is on: every client (anonymous ones too) sees the trace id and the "
            "query count/time of its requests, a side channel (e.g. for account enumeration)."
        )
    if s.explain_analyze and s.preset != "dev":
        out.append(
            "explain_analyze is on: EXPLAIN ANALYZE executes the query (inside a rolled-back "
            "transaction)."
        )
    if s.load_from_ui and s.access not in ("local",):
        out.append(
            "load_from_ui is on: anyone with access to Redline can start a load test "
            "against this application."
        )
    if s.checks_from_ui is True and s.access not in ("local",):
        out.append(
            "checks_from_ui is on: anyone with access to Redline can start refactor-check runs "
            "(sequential GET requests) against this application."
        )
    if not s.history_retention_days:
        out.append("history_retention_days is off: the history tables grow forever.")
    if not s.max_rows and not s.max_age_days:
        out.append("max_rows and max_age_days are both off: stored traces are never pruned.")
    disabled = [f for f in FEATURES if not s.feature(f)]
    if disabled and s.access == "open":
        pass  # covered above
    return out


def _is_wide(entry: str) -> bool:
    import ipaddress

    try:
        net = ipaddress.ip_network(entry.strip(), strict=False)
    except ValueError:
        return False
    if net.num_addresses > 2 ** (24 if net.version == 4 else 72):
        return True
    return not (net.is_private or net.is_loopback)
