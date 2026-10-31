"""Read-only diagnostics for optional instrumentation integrations."""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from typing import Any

_INTEGRATIONS = (
    ("httpx", "httpx", "redline_core.contrib.httpx", "_originals"),
    ("requests", "requests", "redline_core.contrib.requests", "_originals"),
    ("redis", "redis", "redline_core.contrib.redis", "_originals"),
    ("asyncpg", "asyncpg", "redline_core.contrib.asyncpg", "_originals"),
    ("psycopg", "psycopg", "redline_core.contrib.psycopg", "_originals"),
    ("boto3", "boto3", "redline_core.contrib.boto3", "_wrapper"),
)


def _installed(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError, AttributeError):
        return False


def _is_instrumented(module: ModuleType | None, state_name: str) -> bool:
    if module is None:
        return False
    state: Any = getattr(module, state_name, None)
    return bool(state) if state_name == "_originals" else state is not None


def integration_status() -> list[dict[str, bool | str]]:
    """Report installation, module load, and active patch state without importing providers."""
    statuses = []
    for name, provider, contrib, state in _INTEGRATIONS:
        provider_module = sys.modules.get(provider)
        contrib_module = sys.modules.get(contrib)
        statuses.append(
            {
                "name": name,
                "installed": _installed(provider),
                "module_loaded": provider_module is not None,
                "instrumented": _is_instrumented(contrib_module, state),
            }
        )
    return statuses


def integration_warnings(statuses: list[dict[str, bool | str]]) -> list[str]:
    """Explain installed but inactive integrations; this is not an activity report."""
    inactive = [
        str(item["name"]) for item in statuses if item["installed"] and not item["instrumented"]
    ]
    if not inactive:
        return []
    return [
        "Installed integrations are not instrumented: "
        + ", ".join(inactive)
        + ". Installed does not mean captured."
    ]
