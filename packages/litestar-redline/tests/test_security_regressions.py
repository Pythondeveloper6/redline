"""Regression tests from the security review (H3, M3, M4) through the Litestar adapter.

Each one failed before its fix and passes after it.
"""

from __future__ import annotations

import asyncio
import time

from litestar import Litestar, get
from litestar.connection import ASGIConnection
from litestar.testing import TestClient

from litestar_redline import RedlinePlugin

from .conftest import client

REMOTE = ("203.0.113.9", 4444)
TOKEN = "z" * 32


@get("/", sync_to_thread=False)
def home() -> dict:
    time.sleep(0.05)  # long enough for the profiler to take samples
    return {"ok": 1}


def raw_get(app, path, peer, host=b"app.example.com", server=("10.0.0.1", 80)):
    out = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", host)],
        "client": peer,
        "server": server,
    }
    asyncio.run(app(scope, receive, send))
    return out["status"]


def test_h3_unset_access_answers_404_outside_debug_mode(tmp_path):
    plugin = RedlinePlugin(settings={"storage_path": str(tmp_path / "a.db")})
    app = Litestar(route_handlers=[home], plugins=[plugin])
    assert raw_get(app, "/_redline/api/meta", ("127.0.0.1", 5000), b"127.0.0.1:8000") == 404
    assert raw_get(app, "/_redline/api/meta", None, b"localhost") == 404
    plugin.close()
    debug_plugin = RedlinePlugin(settings={"storage_path": str(tmp_path / "b.db")})
    debug_app = Litestar(route_handlers=[home], plugins=[debug_plugin], debug=True)
    assert raw_get(debug_app, "/_redline/api/meta", ("127.0.0.1", 5000), b"localhost") == 200
    assert raw_get(debug_app, "/_redline/api/meta", None, b"localhost") == 403  # unix socket
    debug_plugin.close()


def test_m3_empty_guards_do_not_open_redline(tmp_path):
    # poc_open_guard.py: RedlinePlugin(guards=[]) used to be "framework" with no check
    plugin = RedlinePlugin(settings={"storage_path": str(tmp_path / "l.db")}, guards=[])
    assert plugin.access.policy == "local"
    app = Litestar(route_handlers=[home], plugins=[plugin])
    assert raw_get(app, "/_redline/api/config", REMOTE) in (403, 404)
    plugin.close()


def test_m3_guards_still_decide(tmp_path):
    async def deny(connection: ASGIConnection, _handler) -> None:
        from litestar.exceptions import NotAuthorizedException

        raise NotAuthorizedException("no")

    plugin = RedlinePlugin(settings={"storage_path": str(tmp_path / "g.db")}, guards=[deny])
    assert plugin.access.policy == "framework"
    app = Litestar(route_handlers=[home], plugins=[plugin])
    assert raw_get(app, "/_redline/api/config", REMOTE) == 401
    plugin.close()


def test_m4_anonymous_profile_flag_is_ignored(tmp_path):
    plugin = RedlinePlugin(
        settings={
            "storage_path": str(tmp_path / "p.db"),
            "access": "token",
            "token": TOKEN,
            "sample_rate": 0.0,
            "always_keep_slower_than_ms": 60_000,
        }
    )
    app = Litestar(route_handlers=[home], plugins=[plugin])

    class Env:
        pass

    env = Env()
    env.app = app
    with client(env, remote=REMOTE) as c:
        for _ in range(5):
            c.get("/", headers={"X-Redline-Profile": "1"})
            c.get("/?_redline=profile")
        plugin.store.flush(5)
        assert plugin.store.count() == 0
        c.get("/", headers={"X-Redline-Profile": "1", "Authorization": f"Bearer {TOKEN}"})
        plugin.store.flush(5)
        assert plugin.store.count() == 1


def test_m5_load_target_uses_scope_server(tmp_path, monkeypatch):
    plugin = RedlinePlugin(
        settings={
            "storage_path": str(tmp_path / "r.db"),
            "access": "token",
            "token": TOKEN,
            "load_from_ui": True,
        }
    )
    seen = []
    plugin.api.run_launcher = lambda config, target: seen.append(target) or "r1"
    app = Litestar(route_handlers=[home], plugins=[plugin])
    body = {
        "name": "x",
        "requests": [{"path": "/"}],
        "profile": {"mode": "constant", "rps": 1, "duration_s": 1},
    }
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        r = c.post(
            "/_redline/api/runs",
            json=body,
            headers={"Host": "127.0.0.1:8500", "Authorization": f"Bearer {TOKEN}"},
        )
    assert r.status_code == 202, r.text
    assert seen == ["http://127.0.0.1:8000"]
