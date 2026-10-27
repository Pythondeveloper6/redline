"""Run configs never keep raw secrets: the store, the API and the results see them masked;
only the background process gets them, through a private, short-lived scenario file."""

from __future__ import annotations

import json
import os
import stat

import pytest

from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.load import MemorySink, control, run
from redline_core.load.sinks import redact_config
from redline_core.redact import MASK
from redline_core.storage import SQLiteStore

SECRET = "Bearer sk-live-123"


def config(**extra):
    return {
        "name": "r",
        "headers": {"Authorization": SECRET, "X-Auth-Token": "tok-1", "Accept": "json"},
        "requests": [
            {
                "method": "POST",
                "path": "/login?api_key=k-2&page=1",
                "headers": {"Cookie": "sessionid=s-3", "X-Trace": "visible"},
                "json": {"user": "bob", "password": "pw-4", "items": [{"token": "t-5"}]},
            },
            {
                "method": "POST",
                "path": "/form",
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                "body": "user=bob&password=pw-6",
            },
        ],
        "profile": {"mode": "constant", "rps": 1, "duration_s": 1},
        **extra,
    }


RAW = ("sk-live-123", "tok-1", "k-2", "s-3", "pw-4", "t-5", "pw-6", "pw-7")


def assert_masked(cfg) -> None:
    text = json.dumps(cfg)
    assert not [s for s in RAW if s in text], text


def test_redact_config_masks_headers_bodies_and_urls():
    cfg = redact_config(config(base_url="http://bob:pw-7@127.0.0.1:8000"))
    assert_masked(cfg)
    assert cfg["headers"] == {"Authorization": MASK, "X-Auth-Token": MASK, "Accept": "json"}
    first, second = cfg["requests"]
    assert first["headers"] == {"Cookie": MASK, "X-Trace": "visible"}
    assert first["json"] == {"user": "bob", "password": MASK, "items": [{"token": MASK}]}
    assert "page=1" in first["path"] and "bob" in second["body"]
    assert config()["headers"]["Authorization"] == SECRET  # the input is left alone


def test_launcher_preinserts_a_redacted_config(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "r.db", background=False)
    seen = {}

    def fake_launch(cfg, base_url, store_path, **kw):
        seen["config"] = cfg
        return "run1"

    monkeypatch.setattr(control, "launch_background", fake_launch)
    settings = Settings(load_from_ui=True)
    launcher, stopper = control.run_hooks(store, settings)
    api = RedlineAPI(store, settings, run_launcher=launcher, run_stopper=stopper)
    status, _ = api.dispatch("POST", "api/runs", body=config(), base_url="http://127.0.0.1:8000")
    assert status == 202
    assert seen["config"]["headers"]["Authorization"] == SECRET  # the child needs it raw
    assert_masked(store.get_run("run1").config)
    status, payload = api.dispatch("GET", "api/runs")
    assert status == 200 and payload["items"]
    assert_masked(payload["items"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_scenario_file_is_private_and_removed_by_cleanup(tmp_path):
    store = tmp_path / "r.db"
    # A "python" that exits at once: only the file written before the spawn matters here.
    run_id = control.launch_background(config(), "http://127.0.0.1:1", store, python="/bin/echo")
    path = control.runs_dir(store) / f"{run_id}.scenario.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["headers"]["Authorization"] == SECRET
    control.cleanup(run_id, store)
    assert not path.exists()


@pytest.mark.slow
def test_run_results_keep_a_redacted_config():
    sink = MemorySink()
    sc = config(base_url="http://127.0.0.1:9", timeout_s=1)
    sc["requests"] = sc["requests"][:1]
    result = run(sc, sink=sink, workers=1)
    assert_masked(sink.meta.config)
    assert_masked(result.config)
