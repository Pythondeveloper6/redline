"""The shared access policies (CONTRACT.md section 18) through the Litestar adapter."""

from __future__ import annotations

import base64

import pytest
from litestar.exceptions import NotAuthorizedException

from redline_core.access import SESSION_COOKIE, hash_password
from redline_core.conf import SettingsError

from .conftest import client

TOKEN = "tok-" + "x" * 30
HASH = hash_password("pw", iterations=1000)
REMOTE = ("203.0.113.9", 4444)


def basic(user="ann", password="pw"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_local_is_the_default(make_env):
    env = make_env(guards=None)
    assert env.redline.access.policy == "local"
    with client(env) as c:
        assert c.get("/_redline/api/meta").json()["access"]["policy"] == "local"
        r = c.get("/_redline/api/meta", headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 403 and "proxy" in r.json()["error"]["message"]
    with client(env, remote=REMOTE) as c:
        assert c.get("/_redline/api/meta").json()["error"]["code"] == "forbidden"
        assert c.get("/_redline/badge.js").status_code == 403


def test_token_policy(make_env):
    env = make_env(guards=None, settings={"access": "token", "token": TOKEN})
    with client(env, remote=REMOTE, base_url="https://redline.example") as c:
        assert c.get("/_redline/").status_code == 200  # the shell is public
        r = c.get("/_redline/api/meta", headers={"X-Redline-UI": "1"})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "login_required"
        assert r.json()["error"]["login"] == "token"
        assert "www-authenticate" not in r.headers
        assert c.get("/_redline/api/meta").headers["www-authenticate"].startswith("Bearer")
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert c.get("/_redline/api/meta", headers=auth).status_code == 200
        evil = c.post(
            "/_redline/api/login", json={"token": TOKEN}, headers={"Origin": "https://evil.ex"}
        )
        assert evil.json()["error"]["code"] == "csrf_failed"
        assert c.post("/_redline/api/login", json={"token": "x"}).status_code == 401
        r = c.post("/_redline/api/login", json={"token": TOKEN})
        assert r.status_code == 200 and r.json()["ok"] is True
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/_redline" in cookie
        assert c.get("/_redline/api/meta").status_code == 200
        assert c.get("/_redline/api/config").json()["settings"]["token"] == "***"
        assert c.post("/_redline/api/logout").status_code == 200
        assert c.get("/_redline/api/meta").status_code == 401


def test_basic_policy(make_env):
    env = make_env(guards=None, settings={"access": "basic", "basic_users": {"ann": HASH}})
    with client(env, remote=REMOTE) as c:
        r = c.get("/_redline/api/traces")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        assert c.get("/_redline/api/traces", headers=basic()).status_code == 200
        assert c.get("/_redline/api/traces", headers=basic(password="x")).status_code == 401
        r = c.post("/_redline/api/login", json={"username": "ann", "password": "pw"})
        assert r.status_code == 200 and SESSION_COOKIE in r.headers["set-cookie"]
        assert c.get("/_redline/api/traces").status_code == 200


def test_guards_imply_framework(make_env):
    async def deny(connection, handler):
        raise NotAuthorizedException("no")

    env = make_env(guards=[deny])
    assert env.redline.access.policy == "framework"
    with client(env, remote=REMOTE) as c:
        assert c.get("/_redline/api/meta").status_code == 401
    env = make_env(guards=[deny], settings={"access": "token", "token": TOKEN})
    assert env.redline.access.policy == "token"


def test_open_refused_without_env(make_env, monkeypatch):
    monkeypatch.delenv("REDLINE_ALLOW_OPEN", raising=False)
    env = make_env(guards=None, settings={"access": "open"})
    with client(env, remote=REMOTE) as c:
        assert c.get("/_redline/api/meta").json()["error"]["code"] == "open_refused"
    monkeypatch.setenv("REDLINE_ALLOW_OPEN", "1")
    env = make_env(guards=None, settings={"access": "open"})
    with client(env, remote=REMOTE, base_url="http://anything") as c:
        assert c.get("/_redline/api/meta").status_code == 200


def test_read_only_features_and_ui_disabled(make_env):
    env = make_env(guards=None, settings={"read_only": True, "features": {"sse": False}})
    with client(env) as c:
        assert c.delete("/_redline/api/traces").json()["error"]["code"] == "read_only"
        assert c.get("/_redline/api/stream").json()["error"]["code"] == "feature_disabled"
    env = make_env(guards=None, settings={"ui_enabled": False})
    with client(env) as c:
        assert c.get("/_redline/api/meta").status_code == 404


def test_bad_access_config_fails_at_construction(make_env):
    with pytest.raises(SettingsError):
        make_env(guards=None, settings={"access": "basic"})
