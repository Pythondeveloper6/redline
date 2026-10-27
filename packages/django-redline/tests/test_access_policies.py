"""The shared access policies (CONTRACT.md section 18) through the Django adapter."""

from __future__ import annotations

import base64
import json

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client

from django_redline.runtime import get_runtime
from redline_core.access import SESSION_COOKIE, hash_password

TOKEN = "tok-" + "x" * 30
HASH = hash_password("pw", iterations=1000)
BASE = {"STORAGE_PATH": ":memory:", "SAMPLE_RATE": 1.0}
REMOTE = {"REMOTE_ADDR": "203.0.113.9"}


@pytest.fixture
def redline(settings):
    def apply(**extra):
        settings.REDLINE = {**BASE, **extra}
        return get_runtime()

    return apply


def basic(user="ann", password="pw"):
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"HTTP_AUTHORIZATION": f"Basic {raw}"}


def test_framework_staff_is_the_default(redline, staff_client, db):
    rt = redline()
    assert rt.access.policy == "framework"
    assert rt.config.sources["access"] == "adapter"
    client = Client()
    assert client.get("/redline/api/meta").json()["error"]["code"] == "forbidden"
    assert client.get("/redline/").status_code == 302  # to the login page
    meta = staff_client.get("/redline/api/meta").json()
    assert meta["access"] == {
        "policy": "framework",
        "login": None,
        "read_only": False,
        "preset": "dev",
    }
    # the preset does not replace the staff check (a native hook counts as explicit-ish)
    rt = redline(PRESET="production", TOKEN=TOKEN)
    assert rt.access.policy == "framework" and rt.settings.read_only is True


def test_badge_js_skips_the_permission(redline, client, db):
    redline()
    r = client.get("/redline/badge.js")
    assert r.status_code in (200, 404)  # 404 only when the bundle is not built
    assert r.status_code != 403


def test_token_policy_and_login_flow(redline, db):
    redline(ACCESS="token", TOKEN=TOKEN)
    c = Client(enforce_csrf_checks=True)
    page = c.get("/redline/", **REMOTE)
    assert page.status_code == 200  # the shell is public, with the CSRF meta tag
    r = c.get("/redline/api/meta", HTTP_X_REDLINE_UI="1", **REMOTE)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "login_required" and r.json()["error"]["login"] == "token"
    assert "WWW-Authenticate" not in r
    assert c.get("/redline/api/meta", **REMOTE)["WWW-Authenticate"].startswith("Bearer")
    bearer = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}
    assert c.get("/redline/api/meta", **bearer, **REMOTE).status_code == 200
    # a Bearer-authenticated DELETE needs no Django CSRF token (browsers never send it)
    assert c.delete("/redline/api/traces", **bearer, **REMOTE).status_code == 200
    # the login is a POST: Django's CSRF token is required, as for every mutation
    no_token = c.post(
        "/redline/api/login",
        json.dumps({"token": TOKEN}),
        content_type="application/json",
        **REMOTE,
    )
    assert no_token.status_code == 403 and no_token.json()["error"]["code"] == "csrf_failed"
    csrf = c.cookies["csrftoken"].value
    bad = c.post(
        "/redline/api/login",
        json.dumps({"token": "nope"}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
        **REMOTE,
    )
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_credentials"
    ok = c.post(
        "/redline/api/login",
        json.dumps({"token": TOKEN}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf,
        **REMOTE,
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True
    cookie = ok.cookies[SESSION_COOKIE]
    assert cookie["httponly"] and cookie["samesite"] == "Strict" and cookie["path"] == "/redline"
    assert c.get("/redline/api/meta", **REMOTE).status_code == 200
    data = c.get("/redline/api/config", **REMOTE).json()
    assert data["settings"]["token"] == "***" and TOKEN not in json.dumps(data)
    out = c.post("/redline/api/logout", HTTP_X_CSRFTOKEN=csrf, **REMOTE)
    assert out.status_code == 200
    assert c.get("/redline/api/meta", **REMOTE).status_code == 401


def test_basic_policy(redline, db):
    redline(ACCESS="basic", BASIC_USERS={"ann": HASH})
    c = Client()
    r = c.get("/redline/api/traces", **REMOTE)
    assert r.status_code == 401 and r["WWW-Authenticate"].startswith("Basic")
    assert c.get("/redline/api/traces", **basic(), **REMOTE).status_code == 200
    assert c.get("/redline/api/traces", **basic(password="x"), **REMOTE).status_code == 401
    ok = c.post(
        "/redline/api/login",
        json.dumps({"username": "ann", "password": "pw"}),
        content_type="application/json",
        **REMOTE,
    )
    assert ok.status_code == 200 and SESSION_COOKIE in ok.cookies
    assert c.get("/redline/api/traces", **REMOTE).status_code == 200


def test_local_policy(redline, db):
    redline(ACCESS="local")
    c = Client()
    assert c.get("/redline/api/meta", HTTP_HOST="localhost").status_code == 200
    assert c.get("/redline/api/meta", HTTP_HOST="localhost", **REMOTE).status_code == 403
    r = c.get("/redline/", HTTP_HOST="localhost", **REMOTE)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"  # no login redirect


def test_open_refused_without_env(redline, monkeypatch, db):
    monkeypatch.delenv("REDLINE_ALLOW_OPEN", raising=False)
    redline(ACCESS="open")
    r = Client().get("/redline/api/meta", **REMOTE)
    assert r.status_code == 403 and r.json()["error"]["code"] == "open_refused"


def test_custom_policy_with_dotted_path(redline, db):
    redline(ACCESS="custom", ACCESS_CHECK="tests.test_access_policies.allow_header")
    c = Client()
    assert c.get("/redline/api/meta", **REMOTE).status_code == 403
    assert c.get("/redline/api/meta", HTTP_X_ADMIN="1", **REMOTE).status_code == 200


def allow_header(info):
    return info.headers.get("x-admin") == "1"


def test_read_only_and_features(redline, staff_client):
    redline(READ_ONLY=True, FEATURES={"sse": False})
    assert staff_client.delete("/redline/api/traces").json()["error"]["code"] == "read_only"
    assert staff_client.get("/redline/api/stream").json()["error"]["code"] == "feature_disabled"
    assert staff_client.get("/redline/api/meta").json()["features"]["sse"] is False


def test_ui_disabled(redline, staff_client):
    redline(UI_ENABLED=False)
    assert staff_client.get("/redline/").status_code == 404
    assert staff_client.get("/redline/api/meta").status_code == 404


def test_env_layer_and_bad_config(settings, monkeypatch, db):
    monkeypatch.setenv("REDLINE_SLOW_QUERY_MS", "7")
    settings.REDLINE = dict(BASE)
    rt = get_runtime()
    assert rt.settings.slow_query_ms == 7 and rt.config.sources["slow_query_ms"] == "env"
    settings.REDLINE = {**BASE, "ACCESS": "token", "TOKEN_FILE": "/nope/token"}
    with pytest.raises(ImproperlyConfigured, match="token_file"):
        get_runtime()
