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
    ("anthropic", "anthropic", "redline_core.contrib.anthropic", "_originals"),
    ("openai", "openai", "redline_core.contrib.openai", "_originals"),
    ("pymongo", "pymongo", "redline_core.contrib.pymongo", "_originals"),
    ("kafka", "kafka", "redline_core.contrib.kafka", "_originals"),
    ("pika", "pika", "redline_core.contrib.pika", "_originals"),
    (
        "google_cloud_storage",
        "google.cloud.storage",
        "redline_core.contrib.google_cloud_storage",
        "_originals",
    ),
    ("azure_blob", "azure.storage.blob", "redline_core.contrib.azure_blob", "_originals"),
    ("threads", "concurrent.futures", "redline_core.contrib.threads", "_originals"),
)

#: Integrations that are only ever switched on deliberately. ``threads`` is standard library
#: (always "installed") and changes how threads behave, so an inactive one is not a warning
#: and ``auto_instrument`` never turns it on.
_OPT_IN_ONLY = frozenset({"threads"})


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
    statuses += _dbapi_status()
    return statuses


#: DB-API drivers whose own ``connect()`` bypasses SQLAlchemy. Not auto-instrumented: SQLAlchemy
#: also calls ``connect()``, so wrapping a driver an engine uses would record its SQL twice.
_DBAPI_DRIVERS = ("psycopg2", "pymysql", "MySQLdb", "pyodbc", "pymssql", "oracledb", "cx_Oracle")


def _dbapi_status() -> list[dict[str, bool | str]]:
    """One row per loaded raw driver: statements sent through its own ``connect()`` are a
    blind spot until ``instrument_dbapi(module)`` is called (``source="raw_cursor"`` spans)."""
    return [
        {
            "name": f"dbapi:{name}",
            "installed": True,
            "module_loaded": True,
            "instrumented": getattr(sys.modules[name], "_redline_original_connect", None)
            is not None,
        }
        for name in _DBAPI_DRIVERS
        if name in sys.modules
    ]


def integration_warnings(statuses: list[dict[str, bool | str]]) -> list[str]:
    """Explain installed but inactive integrations; this is not an activity report."""
    inactive = [
        str(item["name"])
        for item in statuses
        if item["installed"]
        and not item["instrumented"]
        and item["name"] not in _OPT_IN_ONLY
        and not str(item["name"]).startswith("dbapi:")
    ]
    if not inactive:
        return []
    return [
        "Installed integrations are not instrumented: "
        + ", ".join(inactive)
        + ". Installed does not mean captured."
    ]
