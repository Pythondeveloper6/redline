"""The shared access policies (CONTRACT.md section 18) through the Flask adapter."""

from __future__ import annotations

import base64

import pytest

from redline_core.access import SESSION_COOKIE, hash_password
from redline_core.conf import SettingsError

TOKEN = "tok-" + "x" * 30
HASH = hash_password("pw", iterations=1000)
REMOTE = {"REMOTE_ADDR": "203.0.113.9"}
HTTPS_REMOTE = {**REMOTE, "wsgi.url_scheme": "https"}


def basic(user="ann", password="pw"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_local_is_the_default(make_env):
    env = make_env()
    assert env.redline._runtime().access.policy == "local"
    c = env.client()
    assert c.get("/_redline/api/meta").json["access"]["policy"] == "local"
    r = c.get("/_redline/api/meta", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403 and "proxy" in r.json["error"]["message"]
    assert c.get("/_redline/badge.js", environ_overrides=REMOTE).status_code == 403


def test_token_policy(make_env):
    env = make_env(settings={"access": "token", "token": TOKEN})
    c = env.client()
    kw = {"environ_overrides": HTTPS_REMOTE, "base_url": "https://redline.example"}
    assert c.get("/_redline/", **kw).status_code == 200  # the shell is public
    r = c.get("/_redline/api/meta", headers={"X-Redline-UI": "1"}, **kw)
    assert r.status_code == 401
    assert r.json["error"] == {
        "code": "login_required",
        "message": "Log in to Redline.",
        "login": "token",
    }
    assert "WWW-Authenticate" not in r.headers
    assert c.get("/_redline/api/meta", **kw).headers["WWW-Authenticate"].startswith("Bearer")
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert c.get("/_redline/api/meta", headers=auth, **kw).status_code == 200
    evil = c.post(
        "/_redline/api/login", json={"token": TOKEN}, headers={"Origin": "https://evil.ex"}, **kw
    )
    assert evil.json["error"]["code"] == "csrf_failed"
    assert c.post("/_redline/api/login", json={"token": "x"}, **kw).status_code == 401
    r = c.post(
        "/_redline/api/login",
        json={"token": TOKEN},
        headers={"Origin": "https://redline.example"},
        **kw,
    )
    assert r.status_code == 200 and r.json["ok"] is True
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/_redline" in cookie
    assert c.get("/_redline/api/meta", **kw).status_code == 200
    assert c.get("/_redline/api/config", **kw).json["settings"]["token"] == "***"
    assert c.post("/_redline/api/logout", **kw).status_code == 200
    assert c.get("/_redline/api/meta", **kw).status_code == 401


def test_basic_policy(make_env):
    env = make_env(settings={"access": "basic", "basic_users": {"ann": HASH}})
    c = env.client()
    r = c.get("/_redline/api/traces", environ_overrides=REMOTE)
    assert r.status_code == 401 and r.headers["WWW-Authenticate"].startswith("Basic")
    assert c.get("/_redline/api/traces", headers=basic(), environ_overrides=REMOTE).status_code == (
        200
    )
    bad = c.get("/_redline/api/traces", headers=basic(password="x"), environ_overrides=REMOTE)
    assert bad.status_code == 401
    r = c.post(
        "/_redline/api/login",
        json={"username": "ann", "password": "pw"},
        environ_overrides=REMOTE,
    )
    assert r.status_code == 200 and SESSION_COOKIE in r.headers["Set-Cookie"]
    assert c.get("/_redline/api/traces", environ_overrides=REMOTE).status_code == 200


def test_permission_implies_framework(make_env):
    env = make_env(permission=lambda request: False)
    rt = env.redline._runtime()
    assert rt.access.policy == "framework" and rt.config.sources["access"] == "adapter"
    env = make_env(permission=lambda request: True, settings={"access": "token", "token": TOKEN})
    assert env.redline._runtime().access.policy == "token"
    assert env.client().get("/_redline/api/meta").status_code == 401


def test_open_refused_without_env(make_env, monkeypatch):
    monkeypatch.delenv("REDLINE_ALLOW_OPEN", raising=False)
    env = make_env(settings={"access": "open"})
    r = env.client().get("/_redline/api/meta", environ_overrides=REMOTE)
    assert r.status_code == 403 and r.json["error"]["code"] == "open_refused"
    monkeypatch.setenv("REDLINE_ALLOW_OPEN", "1")
    env = make_env(settings={"access": "open"})
    assert env.client().get("/_redline/api/meta", environ_overrides=REMOTE).status_code == 200


def test_read_only_features_and_ui_disabled(make_env):
    env = make_env(settings={"read_only": True, "features": {"snapshot": False}})
    c = env.client()
    assert c.delete("/_redline/api/traces").json["error"]["code"] == "read_only"
    r = c.get("/_redline/api/traces/abc/snapshot")
    assert r.status_code == 404 and r.json["error"]["code"] == "feature_disabled"
    env = make_env(settings={"ui_enabled": False})
    assert env.client().get("/_redline/").status_code == 404


def test_app_config_layer_and_mount_path(make_env):
    env = make_env(settings={"mount_path": "/perf"})
    assert env.redline.url_prefix == "/perf"
    assert env.client().get("/perf/api/meta").status_code == 200


def test_bad_access_config_fails_at_init(make_env):
    with pytest.raises(SettingsError):
        make_env(settings={"access": "custom"})
