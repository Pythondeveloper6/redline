"""Read-only production recording-policy checks for ``redline doctor``."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path
from typing import Any


def production_checks(resolved: Any) -> list[tuple[str, str, str]]:
    """Return policy findings without changing settings or probing the live app."""
    settings = resolved.settings
    checks: list[tuple[str, str, str]] = []

    def require(condition: bool, name: str, message: str) -> None:
        checks.append(("ok" if condition else "fail", name, message))

    require(settings.preset == "production", "preset", "preset must be production")
    if settings.enabled:
        checks.append(("ok", "enabled", "recording is enabled"))
    else:
        checks.append(
            ("warn", "enabled", "recording is disabled; no request traces will be captured")
        )

    storage_path = settings.storage_path
    private_path = storage_path != ":memory:" and Path(storage_path).expanduser().is_absolute()
    if private_path and not settings.allow_storage_in_repo:
        for placeholder, value in (
            ("{hostname}", socket.gethostname()),
            ("{pid}", str(os.getpid())),
            ("{role}", settings.storage_role),
        ):
            storage_path = storage_path.replace(placeholder, value)
        path = Path(storage_path).expanduser().resolve()
        directory = path.parent
        while not directory.exists() and directory != directory.parent:
            directory = directory.parent
        try:
            directory_mode = stat.S_IMODE(directory.stat().st_mode)
            target_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0
        except OSError:
            private_path = False
        else:
            private_path = not (directory_mode & 0o077) and (
                not path.exists() or not (target_mode & 0o077)
            )
    else:
        private_path = False
    require(
        private_path,
        "storage privacy",
        "storage must use a persistent absolute path in a private directory with private "
        "file permissions",
    )

    require(
        settings.history_retention_days is not None and settings.history_retention_days > 0,
        "retention",
        "history_retention_days must be bounded above zero",
    )
    require(
        any(
            value is not None and value > 0 for value in (settings.max_rows, settings.max_age_days)
        ),
        "retention",
        "at least one of max_rows or max_age_days must be bounded above zero",
    )
    if settings.sample_rate == 1:
        checks.append(
            ("warn", "sampling", "sample_rate is 1; every eligible request may be recorded")
        )
    else:
        checks.append(("ok", "sampling", "sample_rate is below 1"))

    if settings.access in ("local", "open"):
        require(False, "access", f"access={settings.access!r} is not suitable for production")
    elif settings.access in ("token", "basic"):
        checks.append(("ok", "access", f"access={settings.access!r} requires authentication"))
        require(
            isinstance(settings.secret_key, str) and len(settings.secret_key) >= 32,
            "session signing",
            "secret_key must contain at least 32 characters for signed login sessions",
        )
    elif settings.access in ("framework", "custom"):
        checks.append(
            (
                "warn",
                "access hook",
                f"access={settings.access!r} is provided by application code and cannot be "
                "validated by this command",
            )
        )
    else:
        require(False, "access", "access policy is not recognized")

    require(settings.read_only, "read only", "read_only must be enabled")
    require(
        settings.cookie_secure is True, "secure cookies", "cookie_secure must be explicitly true"
    )
    require(
        not {"*", "0.0.0.0/0", "::/0"}.intersection(settings.trusted_proxies),
        "trusted proxies",
        "trusted_proxies must not trust every network address",
    )

    capture_fields = (
        "capture_request_body",
        "capture_response_body",
        "capture_sql_params",
        "capture_locals",
        "capture_logs",
        "capture_error_details",
    )
    for name in capture_fields:
        require(getattr(settings, name) is False, name, f"{name} must be disabled")

    require(settings.profile == "off", "profile", "profile must be off")
    require(
        settings.profile_sample_rate == 0,
        "profile sampling",
        "profile_sample_rate must be zero",
    )
    require(settings.badge is False, "badge", "badge must be disabled")
    require(settings.server_timing is False, "server timing", "server_timing must be disabled")

    for feature in ("explain", "profile", "replay", "replay_writes", "load", "checks"):
        require(
            not settings.feature(feature),
            f"feature {feature}",
            f"{feature} feature must be disabled",
        )
    for name in ("load_from_ui", "checks_from_ui", "analysis_from_ui"):
        require(getattr(settings, name) is False, name, f"{name} must be disabled")

    return checks
