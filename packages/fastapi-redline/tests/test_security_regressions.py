"""Regression tests built from the security review's proofs of concept (H3, M3, M4, M5).

Each one failed before its fix and passes after it.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from fastapi_redline import install
from redline_core.access import SESSION_COOKIE

REMOTE = ("203.0.113.9", 4444)
TOKEN = "z" * 32


def stored(rl, expected, timeout=5.0):
    """Stored trace count once it reaches ``expected`` (or the timeout passes).

    The trace is finished just after the response is sent, so the request can return before
    it is queued for the writer; one ``flush()`` is not enough on a slow machine.
    """
    deadline = time.monotonic() + timeout
    while True:
        rl.store.flush(timeout)
        count = rl.store.count()
        if count >= expected or time.monotonic() > deadline:
            return count
        time.sleep(0.02)


def raw_get(app, path, client, host, extra=(), server=("127.0.0.1", 8000)):
    """One request straight through ASGI, so the peer can be a unix socket (``None``)."""
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
        "headers": [(b"host", host), *extra],
        "client": client,
        "server": server,
    }
    asyncio.run(app(scope, receive, send))
    return out["status"]


# ── H3 (poc_local_proxy.py) ───────────────────────────────────────────────────────────


def test_h3_fresh_install_behind_a_same_host_proxy_is_not_exposed(tmp_path, caplog):
    app = FastAPI()
    with caplog.at_level("ERROR", logger="redline"):
        install(app, settings={"storage_path": str(tmp_path / "a.db")})  # defaults only
    assert "answers 404" in caplog.text
    api = "/_redline/api/traces"
    # nginx `proxy_pass http://127.0.0.1:8000;` with no proxy_set_header
    assert raw_get(app, api, ("127.0.0.1", 51000), b"127.0.0.1:8000") == 404
    # nginx `proxy_pass http://unix:/run/app.sock;`
    assert raw_get(app, api, None, b"localhost") == 404
    # with proxy_params (X-Forwarded-For)
    xff = [(b"x-forwarded-for", b"203.0.113.9")]
    assert raw_get(app, api, ("127.0.0.1", 51000), b"app.example.com", xff) == 404


def test_h3_debug_mode_keeps_the_local_default(tmp_path):
    app = FastAPI(debug=True)
    install(app, settings={"storage_path": str(tmp_path / "a.db")})
    api = "/_redline/api/traces"
    assert raw_get(app, api, ("127.0.0.1", 51000), b"127.0.0.1:8000") == 200
    assert raw_get(app, api, None, b"localhost") == 403  # a unix socket is not local


def test_h3_unix_socket_needs_trust_unix_socket(tmp_path):
    for trust, status in ((False, 403), (True, 200)):
        app = FastAPI()
        settings = {
            "storage_path": str(tmp_path / f"{trust}.db"),
            "access": "local",
            "trust_unix_socket": trust,
        }
        install(app, settings=settings)
        assert raw_get(app, "/_redline/api/traces", None, b"localhost") == status


# ── M3 (poc_open_guard.py) ────────────────────────────────────────────────────────────


def test_m3_empty_dependencies_do_not_open_redline(tmp_path):
    app = FastAPI()
    rl = install(app, settings={"storage_path": str(tmp_path / "a.db")}, dependencies=[])
    assert rl.access.policy == "local"  # no hook: the default rules, not "framework"
    with TestClient(app, client=REMOTE) as c:
        r = c.get("/_redline/api/config", headers={"host": "app.example.com"})
        assert r.status_code in (403, 404)


def test_m3_dependency_returning_false_refuses(tmp_path):
    def is_admin(request: Request) -> bool:  # returns False instead of raising
        return request.headers.get("x-admin") == "1"

    app = FastAPI()
    rl = install(app, settings={"storage_path": str(tmp_path / "b.db")}, dependencies=[is_admin])
    assert rl.access.policy == "framework"
    with TestClient(app, client=REMOTE) as c:
        hdr = {"host": "app.example.com"}
        r = c.get("/_redline/api/traces", headers=hdr)
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
        assert c.get("/_redline/api/traces", headers={**hdr, "x-admin": "1"}).status_code == 200


def test_m3_open_still_needs_the_env_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("REDLINE_ALLOW_OPEN", raising=False)
    app = FastAPI()
    install(app, settings={"storage_path": str(tmp_path / "c.db"), "access": "open"})
    with TestClient(app, client=REMOTE) as c:
        assert c.get("/_redline/api/traces", headers={"host": "x.example"}).status_code == 403


# ── M4 (poc_force_profile.py) ─────────────────────────────────────────────────────────


def _profiled_app(tmp_path, **settings):
    app = FastAPI()

    @app.get("/")
    def home():
        return {"ok": 1}

    cfg = {"storage_path": str(tmp_path / "p.db"), "access": "token", "token": TOKEN}
    return app, install(app, settings={**cfg, **settings})


def test_m4_production_preset_cannot_be_forced(tmp_path):
    app, rl = _profiled_app(tmp_path, preset="production", sample_rate=0.0)
    with TestClient(app, client=REMOTE) as c:
        for _ in range(20):
            c.get("/", headers={"X-Redline-Profile": "1"})
        rl.store.flush(5)
        assert rl.settings.profile == "off"
        assert rl.store.count() == 0


def test_m4_anonymous_flag_is_ignored_but_credentials_force(tmp_path):
    app, rl = _profiled_app(tmp_path, sample_rate=0.0, always_keep_slower_than_ms=60_000)
    with TestClient(app, client=REMOTE) as c:
        for _ in range(10):
            c.get("/", headers={"X-Redline-Profile": "1"})
            c.get("/?_redline=profile")
        rl.store.flush(5)
        assert rl.store.count() == 0  # neither profiled nor stored
        c.get("/", headers={"X-Redline-Profile": "1", "Authorization": f"Bearer {TOKEN}"})
        assert stored(rl, 1) == 1
        # a login cookie counts as well
        login = c.post("/_redline/api/login", json={"token": TOKEN})
        cookie = login.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        c.cookies.set(SESSION_COOKIE, cookie)
        c.get("/?_redline=profile")
        assert stored(rl, 2) == 2


def test_m4_debug_mode_honours_the_flag(tmp_path):
    app, rl = _profiled_app(tmp_path, sample_rate=0.0, always_keep_slower_than_ms=60_000)
    app.debug = True
    with TestClient(app, client=REMOTE) as c:
        c.get("/", headers={"X-Redline-Profile": "1"})
        assert stored(rl, 1) == 1


# ── M5 (poc_load_target.py) ───────────────────────────────────────────────────────────


def test_m5_load_target_ignores_the_host_header(tmp_path):
    app = FastAPI()
    rl = install(
        app,
        settings={
            "storage_path": str(tmp_path / "a.db"),
            "access": "token",
            "token": TOKEN,
            "load_from_ui": True,
        },
    )
    seen = []
    rl.api.run_launcher = lambda config, target: seen.append(target) or "r1"
    body = {
        "name": "x",
        "requests": [{"method": "POST", "path": "/v1/kv/secret", "body": "FLUSHALL\r\n"}],
        "profile": {"mode": "constant", "rps": 50, "duration_s": 10},
    }
    with TestClient(app, client=REMOTE, base_url="http://127.0.0.1:8000") as c:
        r = c.post(
            "/_redline/api/runs",
            json=body,
            headers={"host": "127.0.0.1:8500", "authorization": f"Bearer {TOKEN}"},
        )
    assert r.status_code == 202, r.text
    assert seen == ["http://127.0.0.1:8000"]  # the app's own port, not Host's 8500
