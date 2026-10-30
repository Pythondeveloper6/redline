from __future__ import annotations

import sys
import types

from redline_core import integration_status as status_module


def test_status_reports_installed_loaded_and_instrumented_without_importing_providers(
    monkeypatch,
):
    providers = {"httpx", "requests", "redis", "asyncpg", "psycopg", "boto3"}
    for name in providers:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(
        status_module.importlib.util,
        "find_spec",
        lambda name: object() if name in providers else None,
    )

    loaded = types.ModuleType("httpx")
    monkeypatch.setitem(sys.modules, "httpx", loaded)
    active = types.ModuleType("redline_core.contrib.httpx")
    active._originals = {"send": object()}
    monkeypatch.setitem(sys.modules, "redline_core.contrib.httpx", active)
    inactive = types.ModuleType("redline_core.contrib.requests")
    inactive._originals = {}
    monkeypatch.setitem(sys.modules, "redline_core.contrib.requests", inactive)
    boto = types.ModuleType("redline_core.contrib.boto3")
    boto._wrapper = object()
    monkeypatch.setitem(sys.modules, "redline_core.contrib.boto3", boto)

    statuses = status_module.integration_status()
    assert [item["name"] for item in statuses] == [
        "httpx",
        "requests",
        "redis",
        "asyncpg",
        "psycopg",
        "boto3",
    ]
    assert statuses[0] == {
        "name": "httpx",
        "installed": True,
        "module_loaded": True,
        "instrumented": True,
    }
    assert statuses[1]["installed"] and not statuses[1]["instrumented"]
    assert statuses[2]["installed"] and not statuses[2]["module_loaded"]
    assert statuses[-1]["instrumented"]
    assert all(name not in sys.modules for name in providers - {"httpx"})


def test_find_spec_errors_mean_not_installed(monkeypatch):
    monkeypatch.setattr(
        status_module.importlib.util,
        "find_spec",
        lambda name: (_ for _ in ()).throw(ModuleNotFoundError(name)),
    )
    assert all(not item["installed"] for item in status_module.integration_status())


def test_warnings_only_say_installed_does_not_mean_captured():
    statuses = [
        {"name": "httpx", "installed": True, "module_loaded": False, "instrumented": False},
        {"name": "boto3", "installed": True, "module_loaded": True, "instrumented": True},
    ]
    warnings = status_module.integration_warnings(statuses)
    assert len(warnings) == 1
    assert "httpx" in warnings[0]
    assert "Installed does not mean captured" in warnings[0]
    assert "boto3" not in warnings[0]


def test_health_endpoint_includes_json_friendly_integration_status(store, monkeypatch):
    from redline_core.api import RedlineAPI
    from redline_core.conf import Settings

    monkeypatch.setattr(
        status_module,
        "integration_status",
        lambda: [
            {
                "name": "httpx",
                "installed": True,
                "module_loaded": False,
                "instrumented": False,
            }
        ],
    )
    api = RedlineAPI(store, Settings())
    code, data = api.dispatch("GET", "api/analysis/health")
    assert code == 200
    assert data["integrations"][0]["name"] == "httpx"
    assert "not instrumented" in data["warnings"][-1]
    assert "do not indicate whether calls were observed" in data["note"]
