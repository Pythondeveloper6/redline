"""The shared access policies (CONTRACT.md section 18) through the FastAPI adapter."""

from __future__ import annotations

import base64

import pytest
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient

from redline_core.access import SESSION_COOKIE, hash_password
from redline_core.conf import SettingsError

TOKEN = "tok-" + "x" * 30
HASH = hash_password("pw", iterations=1000)
REMOTE = ("203.0.113.9", 4444)
LOCAL = "http://localhost"


def basic(user="ann", password="pw"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_local_is_the_default(make_env):
    env = make_env(dependencies=None)
    assert env.redline.access.policy == "local"
    with TestClient(env.app, base_url=LOCAL) as c:
        assert c.get("/_redline/api/meta").json()["access"]["policy"] == "local"
    with TestClient(env.app, client=REMOTE) as c:
        assert c.get("/_redline/api/meta").json()["error"]["code"] == "forbidden"
        assert c.get("/_redline/badge.js").status_code == 403
    with TestClient(env.app, base_url=LOCAL) as c:  # a proxy on this machine relays a remote client
        r = c.get("/_redline/api/meta", headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 403 and "proxy" in r.json()["error"]["message"]


def test_local_with_trusted_proxy_and_allowed_ips(make_env):
    env = make_env(
        dependencies=None,
        settings={"trusted_proxies": ["127.0.0.1"], "allowed_ips": ["198.51.100.0/24"]},
    )
    with TestClient(env.app, base_url=LOCAL) as c:
        ok = c.get("/_redline/api/meta", headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.4"})
        assert ok.status_code == 200
        r = c.get("/_redline/api/meta", headers={"X-Forwarded-For": "198.51.100.4, 6.6.6.6"})
        assert r.status_code == 403


def test_token_policy(make_env):
    env = make_env(dependencies=None, settings={"access": "token", "token": TOKEN})
    with TestClient(env.app, client=REMOTE, base_url="https://redline.example") as c:
        assert c.get("/_redline/").status_code == 200  # the shell is public
        r = c.get("/_redline/api/meta", headers={"X-Redline-UI": "1"})
        assert r.status_code == 401
        assert r.json()["error"] == {
            "code": "login_required",
            "message": "Log in to Redline.",
            "login": "token",
        }
        assert "www-authenticate" not in r.headers and r.headers["x-frame-options"] == "DENY"
        assert c.get("/_redline/api/meta").headers["www-authenticate"].startswith("Bearer")
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert c.get("/_redline/api/meta", headers=auth).status_code == 200
        assert c.get("/_redline/api/stream", headers={"X-Redline-UI": "1"}).status_code == 401
        bad = c.post("/_redline/api/login", json={"token": "nope"})
        assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_credentials"
        evil = c.post(
            "/_redline/api/login", json={"token": TOKEN}, headers={"Origin": "https://evil.ex"}
        )
        assert evil.json()["error"]["code"] == "csrf_failed"
        r = c.post(
            "/_redline/api/login",
            json={"token": TOKEN},
            headers={"Origin": "https://redline.example"},
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" in cookie
        assert "Path=/_redline" in cookie
        assert c.get("/_redline/api/meta").status_code == 200  # the cookie works
        assert c.get("/_redline/api/config").json()["settings"]["token"] == "***"
        assert c.post("/_redline/api/logout").status_code == 200
        assert c.get("/_redline/api/meta").status_code == 401


def test_basic_policy(make_env):
    env = make_env(dependencies=None, settings={"access": "basic", "basic_users": {"ann": HASH}})
    with TestClient(env.app, client=REMOTE) as c:
        r = c.get("/_redline/api/traces")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        assert r.json()["error"]["login"] == "basic"
        assert c.get("/_redline/api/traces", headers=basic()).status_code == 200
        assert c.get("/_redline/api/traces", headers=basic(password="x")).status_code == 401
        r = c.post("/_redline/api/login", json={"username": "ann", "password": "pw"})
        assert r.status_code == 200 and SESSION_COOKIE in r.headers["set-cookie"]
        assert c.get("/_redline/api/traces").status_code == 200


def test_login_rate_limit(make_env):
    env = make_env(
        dependencies=None,
        settings={"access": "token", "token": TOKEN, "max_login_attempts": 2},
    )
    with TestClient(env.app, client=REMOTE) as c:
        for _ in range(2):
            c.post("/_redline/api/login", json={"token": "bad"})
        r = c.post("/_redline/api/login", json={"token": TOKEN})
        assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_requests"
        assert int(r.headers["retry-after"]) > 0


def test_dependencies_imply_framework(make_env):
    def deny():
        raise HTTPException(403, "no")

    env = make_env(dependencies=[Depends(deny)])
    assert env.redline.access.policy == "framework"
    assert env.redline.config.sources["access"] == "adapter"
    with TestClient(env.app, base_url=LOCAL) as c:
        assert c.get("/_redline/api/meta").status_code == 403
    # an explicit access setting wins; the dependencies still run on top of it
    env = make_env(
        dependencies=[Depends(lambda: None)], settings={"access": "token", "token": TOKEN}
    )
    assert env.redline.access.policy == "token"
    with TestClient(env.app, base_url=LOCAL) as c:
        assert c.get("/_redline/api/meta").status_code == 401


def test_open_refused_without_env(make_env, monkeypatch):
    monkeypatch.delenv("REDLINE_ALLOW_OPEN", raising=False)
    env = make_env(dependencies=None, settings={"access": "open"})
    with TestClient(env.app, client=REMOTE) as c:
        r = c.get("/_redline/api/meta")
        assert r.status_code == 403 and r.json()["error"]["code"] == "open_refused"
    monkeypatch.setenv("REDLINE_ALLOW_OPEN", "1")
    env = make_env(dependencies=None, settings={"access": "open"})
    with TestClient(env.app, client=REMOTE, base_url="http://anything") as c:
        assert c.get("/_redline/api/meta").status_code == 200


def test_read_only_and_features(make_env):
    env = make_env(
        dependencies=None, settings={"read_only": True, "features": {"sse": False, "load": False}}
    )
    with TestClient(env.app, base_url=LOCAL) as c:
        assert c.delete("/_redline/api/traces").json()["error"]["code"] == "read_only"
        assert c.get("/_redline/api/stream").json()["error"]["code"] == "feature_disabled"
        assert c.get("/_redline/api/runs").status_code == 404
        features = c.get("/_redline/api/meta").json()["features"]
        assert features["sse"] is False and features["load"] is False


def test_ui_disabled_and_mount_path(make_env):
    env = make_env(dependencies=None, settings={"ui_enabled": False})
    with TestClient(env.app, base_url=LOCAL) as c:
        assert c.get("/_redline/").status_code == 404
        assert c.get("/_redline/api/meta").status_code == 404
    env = make_env(dependencies=None, settings={"mount_path": "/perf"})
    assert env.redline.prefix == "/perf"
    with TestClient(env.app, base_url=LOCAL) as c:
        assert c.get("/perf/api/meta").status_code == 200


def test_bad_access_config_fails_at_install(make_env):
    with pytest.raises(SettingsError):
        make_env(dependencies=None, settings={"access": "token"})
    with pytest.raises(SettingsError):
        make_env(dependencies=None, settings={"access": "token", "token_file": "/nope/x"})


def test_env_and_file_layers(make_env, monkeypatch, tmp_path):
    monkeypatch.setenv("REDLINE_ACCESS", "token")
    monkeypatch.setenv("REDLINE_TOKEN", TOKEN)
    env = make_env(dependencies=None)
    assert env.redline.access.policy == "token"
    assert env.redline.config.sources["token"] == "env"
    with TestClient(env.app, base_url=LOCAL) as c:
        data = c.get("/_redline/api/config", headers={"Authorization": f"Bearer {TOKEN}"}).json()
        assert data["sources"]["access"] == "env" and data["settings"]["token"] == "***"
