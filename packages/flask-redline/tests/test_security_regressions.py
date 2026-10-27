"""Regression tests from the security review (H3, M4, M5) through the Flask adapter.

Each one failed before its fix and passes after it.
"""

from __future__ import annotations

import time

from flask import Flask

from flask_redline import Redline

TOKEN = "z" * 32
LOCAL_PROXY = {"REMOTE_ADDR": "127.0.0.1"}  # nginx on this machine, no X-Forwarded-For
UNIX_SOCKET = {"REMOTE_ADDR": ""}
REMOTE = {"REMOTE_ADDR": "203.0.113.9"}


def _app(tmp_path, debug=False, **settings):
    app = Flask(__name__)
    app.debug = debug

    @app.get("/")
    def home():
        time.sleep(0.02)  # long enough for the profiler to take samples
        return {"ok": 1}

    rl = Redline(app, settings={"storage_path": str(tmp_path / "r.db"), **settings})
    return app, rl


def test_h3_unset_access_answers_404_outside_debug_mode(tmp_path, caplog):
    with caplog.at_level("ERROR", logger="redline"):
        app, _ = _app(tmp_path)
    assert "answers 404" in caplog.text
    c = app.test_client()
    for env in (LOCAL_PROXY, UNIX_SOCKET):
        r = c.get("/_redline/api/traces", headers={"Host": "127.0.0.1:8000"}, environ_base=env)
        assert r.status_code == 404
    app.debug = True  # e.g. app.run(debug=True) after Redline(app)
    r = c.get("/_redline/api/traces", headers={"Host": "127.0.0.1:8000"}, environ_base=LOCAL_PROXY)
    assert r.status_code == 200
    r = c.get("/_redline/api/traces", headers={"Host": "localhost"}, environ_base=UNIX_SOCKET)
    assert r.status_code == 403  # a unix-socket peer is not local by default


def test_m4_anonymous_profile_flag_is_ignored(tmp_path):
    app, rl = _app(
        tmp_path, access="token", token=TOKEN, sample_rate=0.0, always_keep_slower_than_ms=60_000
    )
    c = app.test_client()
    for _ in range(5):
        c.get("/", headers={"X-Redline-Profile": "1"}, environ_base=REMOTE, buffered=True)
        c.get("/?_redline=profile", environ_base=REMOTE, buffered=True)
    rl.store.flush(5)
    assert rl.store.count() == 0
    auth = {"X-Redline-Profile": "1", "Authorization": f"Bearer {TOKEN}"}
    c.get("/", headers=auth, environ_base=REMOTE, buffered=True)
    rl.store.flush(5)
    assert rl.store.count() == 1


def test_m5_load_target_uses_server_port_not_host(tmp_path):
    app, rl = _app(tmp_path, access="token", token=TOKEN, load_from_ui=True)
    seen = []
    rl.api.run_launcher = lambda config, target: seen.append(target) or "r1"
    body = {
        "name": "x",
        "requests": [{"path": "/"}],
        "profile": {"mode": "constant", "rps": 1, "duration_s": 1},
    }
    r = app.test_client().post(
        "/_redline/api/runs",
        json=body,
        headers={"Host": "127.0.0.1:8500", "Authorization": f"Bearer {TOKEN}"},
        environ_base=REMOTE,
        environ_overrides={"SERVER_PORT": "5000"},
    )
    assert r.status_code == 202, r.json
    assert seen == ["http://127.0.0.1:5000"]
