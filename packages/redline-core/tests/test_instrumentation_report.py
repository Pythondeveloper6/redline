from __future__ import annotations

import time

from redline_core.instrumentation_report import build_report

from .conftest import make_trace


def test_empty_store_reports_no_traces(store):
    report = build_report(store)
    assert report["traces_sampled"] == 0
    assert report["truncated_traces"] == 0
    assert any("No traces" in w for w in report["warnings"])
    assert report["sampled"] is True


def test_truncated_and_profileless_traces_are_counted(store):
    t = make_trace(started_at=time.time() - 5)
    t.truncated = {"spans": True}
    store.save(t)
    store.save(make_trace(path="/b", route="/b", started_at=time.time() - 4))
    report = build_report(store)
    assert report["traces_sampled"] == 2
    assert report["truncated_traces"] == 1
    assert any("truncated" in w for w in report["warnings"])
    assert report["without_profile"] >= 1


def test_old_traces_outside_window_are_ignored(store):
    store.save(make_trace(started_at=time.time() - 7200))
    assert build_report(store, window=3600)["traces_sampled"] == 0


def test_integration_warning_for_installed_but_inactive(store, monkeypatch):
    from redline_core import instrumentation_report as mod

    monkeypatch.setattr(
        mod,
        "integration_status",
        lambda: [
            {"name": "httpx", "installed": True, "module_loaded": True, "instrumented": False}
        ],
    )
    report = build_report(store)
    assert report["integrations"][0]["name"] == "httpx"
    assert any("httpx" in w for w in report["warnings"])


def test_dropped_writes_come_from_operational_health(store):
    class Fake:
        def operational_health(self):
            return {"status": "degraded", "dropped": 4, "write_failures": 1}

    report = build_report(Fake())
    assert report["dropped_writes"] == 4 and report["write_failures"] == 1
    assert any("dropped" in w for w in report["warnings"])


def test_no_paths_urls_or_secrets_in_output(store):
    import json

    store.save(make_trace(started_at=time.time() - 1))
    blob = json.dumps(build_report(store, runtime_health=lambda: {"status": "ok", "pid": 1}))
    assert "password" not in blob and "postgresql://" not in blob


def test_cli_instrumentation(tmp_path, capsys):
    import json

    from redline_core import cli
    from redline_core.storage.sqlite import SQLiteStore

    path = str(tmp_path / "r.db")
    s = SQLiteStore(path, background=False)
    s.save(make_trace(started_at=time.time() - 1))
    s.close()
    assert cli.main(["instrumentation", "--store", path, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["traces_sampled"] == 1
    assert cli.main(["instrumentation", "--store", path]) == 0
    assert "traces sampled: 1" in capsys.readouterr().out
    assert cli.main(["instrumentation", "--store", path, "--window", "0"]) == 2


def test_api_instrumentation_route(store):
    from redline_core.api import RedlineAPI
    from redline_core.conf import Settings

    store.save(make_trace(started_at=time.time() - 1))
    api = RedlineAPI(store, Settings(), framework="django")
    status, data = api.dispatch("GET", "api/instrumentation")
    assert status == 200 and data["traces_sampled"] == 1
    assert api.dispatch("POST", "api/instrumentation")[0] == 405
