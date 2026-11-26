"""UI-started load runs on the shared PostgreSQL store (no live database needed)."""

from __future__ import annotations

import pytest

from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.load import control
from redline_core.runs import Run
from redline_core.storage import PostgresStore, SQLiteStore

URL = "postgresql://redline:pw-9z@db.internal:5432/obs"


class FakePostgres(PostgresStore):
    """A PostgresStore whose run methods go to SQLite (the engine is shared code)."""

    def __init__(self, tmp_path):  # no connection: only what the hooks use
        self.url = URL
        self.path = "postgresql://redline:***@db.internal:5432/obs"
        self._inner = SQLiteStore(str(tmp_path / "pg-stand-in.db"), background=False)

    def save_run(self, run):
        self._inner.save_run(run)

    def get_run(self, run_id):
        return self._inner.get_run(run_id)

    def finish_run(self, run_id, status, summary=None):
        self._inner.finish_run(run_id, status, summary)


@pytest.fixture
def pg(tmp_path):
    return FakePostgres(tmp_path)


def test_hooks_are_available_for_the_shared_store(pg):
    launcher, stopper = control.run_hooks(pg, Settings(load_from_ui=True))
    assert callable(launcher) and callable(stopper)
    assert control.run_hooks(pg, Settings()) == (None, None)


def test_launch_passes_the_url_to_the_child_not_through_argv(pg, monkeypatch, tmp_path):
    seen = {}

    def fake_launch(config, base_url, store_path=None, **kw):
        seen.update(store_path=store_path, **kw)
        return "run1"

    monkeypatch.setattr(control, "launch_background", fake_launch)
    launcher, _ = control.run_hooks(pg, Settings(load_from_ui=True))
    api = RedlineAPI(pg, Settings(load_from_ui=True), run_launcher=launcher)
    body = {
        "name": "r",
        "requests": [{"method": "GET", "path": "/"}],
        "profile": {"mode": "constant", "rps": 1, "duration_s": 1},
    }
    status, payload = api.dispatch("POST", "api/runs", body=body, base_url="http://127.0.0.1:8000")
    assert status == 202 and payload["id"] == "run1"
    assert seen["store_path"] is None and seen["store_url"] == URL
    assert pg.get_run("run1").status == "running"


def test_launch_background_hands_the_url_over_in_the_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kw):
        captured["cmd"], captured["env"] = cmd, kw.get("env")
        return FakeProc()

    monkeypatch.setattr(control.subprocess, "Popen", fake_popen)
    config = {
        "name": "r",
        "requests": [{"method": "GET", "path": "/"}],
        "profile": {"mode": "constant", "rps": 1, "duration_s": 1},
    }
    control.launch_background(config, "http://127.0.0.1:8000", None, store_url=URL)
    assert not any("pw-9z" in part for part in captured["cmd"])
    assert captured["env"]["REDLINE_STORAGE_URL"] == URL


def test_stop_marks_a_run_whose_process_is_gone_as_failed(pg, monkeypatch):
    pg.save_run(Run(id="gone", name="x", status="running"))
    monkeypatch.setattr(control, "stop_run", lambda run_id, path=None: False)
    _, stopper = control.run_hooks(pg, Settings(load_from_ui=True))
    assert stopper("gone") is False
    run = pg.get_run("gone")
    assert run.status == "failed"
    assert "exited" in run.summary["message"]


def test_stop_leaves_a_live_run_alone(pg, monkeypatch):
    pg.save_run(Run(id="live", name="x", status="running"))
    monkeypatch.setattr(control, "stop_run", lambda run_id, path=None: True)
    _, stopper = control.run_hooks(pg, Settings(load_from_ui=True))
    assert stopper("live") is True
    assert pg.get_run("live").status == "running"
