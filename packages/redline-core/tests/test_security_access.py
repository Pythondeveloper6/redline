"""Regression tests for the access-control review (H3, M4, M5, L2, L3, L5).

Each test failed before the fix it names and passes after it.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time

import pytest

from redline_core.access import SESSION_COOKIE, Access, RequestInfo
from redline_core.api import RedlineAPI, own_base_url
from redline_core.conf import PRESETS, Settings
from redline_core.config import mask_url_userinfo, resolve_settings
from redline_core.runs import Run
from redline_core.storage import SQLiteStore

from .conftest import make_trace

TOKEN = "s3cr3t-" + "x" * 30
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


def req(path="api/meta", *, method="GET", ip="127.0.0.1", host="127.0.0.1:8000", **headers):
    hdrs = {k.replace("_", "-"): v for k, v in headers.items()}
    if host is not None:
        hdrs["host"] = host
    return RequestInfo.build(
        method=method, path=path, client_ip=ip, headers=hdrs, mount="/_redline"
    )


def gate(*, explicit=True, debug=False, **settings) -> Access:
    return Access(
        Settings.from_mapping(settings),
        environ={},
        access_explicit=explicit,
        debug=lambda: debug,
    )


# ── H3: an unset `access` is not "local" outside debug mode ───────────────────────────


def test_h3_unset_access_answers_404_outside_debug_mode():
    locked = gate(explicit=False)
    # nginx `proxy_pass http://127.0.0.1:8000;` without proxy_set_header: loopback peer,
    # loopback Host, no X-Forwarded-For – it used to look local
    assert locked.check(req()).status == 404
    assert locked.check(req("")).status == 404  # the UI shell too
    assert gate(explicit=False, debug=True).check(req()).allowed
    assert gate(explicit=True).check(req()).allowed  # access="local" set on purpose


def test_h3_resolved_config_knows_whether_access_was_set():
    assert not resolve_settings({}, read_file=False, environ={}).access_explicit
    assert resolve_settings({"access": "local"}, read_file=False, environ={}).access_explicit
    env = {"REDLINE_ACCESS": "local"}
    assert resolve_settings({}, read_file=False, environ=env).access_explicit
    warnings = resolve_settings({}, read_file=False, environ={}).warnings
    assert any("access is not set" in w for w in warnings)


def test_h3_error_logged_once_at_startup(caplog):
    g = gate(explicit=False)
    with caplog.at_level(logging.WARNING, logger="redline"):
        g.log_startup("/_redline", "pass dependencies")
        g.log_startup("/_redline", "pass dependencies")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "access" in errors[0].getMessage()


@pytest.mark.parametrize("peer", [None, ""])
def test_h3_unix_socket_peer_is_not_local_by_default(peer):
    # nginx `proxy_pass http://unix:/run/app.sock;` – no client address at all
    assert gate(access="local").check(req(ip=peer, host="localhost")).status == 403
    assert gate(access="local", trust_unix_socket=True).check(req(ip=peer)).allowed


# ── M4: forcing the profiler needs Redline credentials or debug mode ──────────────────


def test_m4_may_force_needs_credentials():
    token = gate(access="token", token=TOKEN)
    remote = {"ip": "203.0.113.9", "host": "app.example.com"}
    assert not token.may_force(req(**remote))
    assert not token.may_force(req(**remote, authorization="Bearer wrong"))
    assert token.may_force(req(**remote, authorization=f"Bearer {TOKEN}"))
    assert token.may_force(req(**remote), debug=True)
    local = gate(access="local")
    assert local.may_force(req())
    assert not local.may_force(req(**remote))
    assert not gate(explicit=False).may_force(req())  # locked: nobody
    assert not gate(access="framework").may_force(req())  # hooks cannot run here


def test_m4_failed_force_attempts_count_towards_the_login_rate_limit():
    g = gate(access="token", token=TOKEN, max_login_attempts=3)
    remote = {"ip": "203.0.113.9", "host": "app.example.com"}
    for _ in range(3):
        assert not g.may_force(req(**remote, authorization="Bearer nope"))
    assert not g.may_force(req(**remote, authorization=f"Bearer {TOKEN}"))  # blocked


def test_m4_production_preset_never_profiles():
    assert PRESETS["production"]["profile"] == "off"
    cfg = resolve_settings({"preset": "production", "token": TOKEN}, read_file=False, environ={})
    assert cfg.settings.profile == "off"


def test_m4_run_tags_only_for_runs_the_store_knows(tmp_path):
    store = SQLiteStore(tmp_path / "r.db", background=False)
    store.save_run(Run(id="real"))
    done = Run(id="old", status="completed", finished_at=time.time() - 3600)
    store.save_run(done)
    for i, run_id in enumerate(("real", "forged", "old")):
        store.save(make_trace(path=f"/{i}", tags={"run_id": run_id, "step": "1", "x": "y"}))
    tags = {t["path"]: t["tags"] for t in store.list(None).items}
    assert tags["/0"] == {"run_id": "real", "step": "1", "x": "y"}
    assert tags["/1"] == {"x": "y"}  # a made-up X-Redline-Run is dropped
    assert tags["/2"] == {"x": "y"}  # the run ended an hour ago
    store.close()


# ── M5: UI-started load runs target the server's own port, not the Host header ───────


def test_m5_own_base_url_uses_the_listening_port():
    assert own_base_url("http", ("0.0.0.0", 8000)) == "http://127.0.0.1:8000"
    assert own_base_url("https", ("::", 8443), "/app/") == "https://[::1]:8443/app"
    assert own_base_url("http", "5000") == "http://127.0.0.1:5000"
    assert own_base_url("http", None) is None  # unix socket: base_url is required
    assert own_base_url("http", ("x", "nope")) is None


def test_m5_runs_without_a_port_need_base_url(tmp_path):
    store = SQLiteStore(tmp_path / "r.db", background=False)
    seen = []
    api = RedlineAPI(
        store,
        Settings(load_from_ui=True),
        run_launcher=lambda config, target: seen.append(target) or "r1",
    )
    profile = {"mode": "constant", "rps": 1, "duration_s": 1}
    body = {"name": "x", "requests": [{"path": "/"}], "profile": profile}
    status, payload = api.dispatch("POST", "api/runs", body=json.dumps(body), base_url=None)
    assert status == 400 and "base_url" in payload["error"]["message"]
    status, _ = api.dispatch(
        "POST", "api/runs", body=json.dumps(body), base_url=own_base_url("http", 8000)
    )
    assert status == 202 and seen == ["http://127.0.0.1:8000"]
    store.close()


# ── L2: the store is private ──────────────────────────────────────────────────────────


@posix_only
def test_l2_store_files_are_owner_only(tmp_path):
    path = tmp_path / ".redline" / "redline.db"
    store = SQLiteStore(path, background=False)
    store.save(make_trace())
    directory = path.parent
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for f in (path, path.with_name("redline.db-wal"), path.with_name("redline.db-shm")):
        if f.exists():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    assert (directory / ".gitignore").read_text().splitlines()[-1] == "*"
    store.close()


@posix_only
def test_l2_existing_gitignore_and_directory_are_kept(tmp_path):
    (tmp_path / ".gitignore").write_text("keep\n")
    os.chmod(tmp_path, 0o755)
    store = SQLiteStore(tmp_path / "r.db", background=False)
    assert (tmp_path / ".gitignore").read_text() == "keep\n"
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755  # not ours: left alone
    assert stat.S_IMODE((tmp_path / "r.db").stat().st_mode) == 0o600
    store.close()
    # an older, world-readable store is tightened when opened
    old = tmp_path / "old.db"
    old.touch()
    os.chmod(old, 0o644)
    SQLiteStore(old, background=False).close()
    assert stat.S_IMODE(old.stat().st_mode) == 0o600


@posix_only
def test_l2_run_files_are_owner_only(tmp_path):
    from redline_core.load import control

    store = tmp_path / ".redline" / "redline.db"
    control.StopFile("abc", store).set()
    control.write_pid("abc", store)
    rdir = control.runs_dir(store)
    assert stat.S_IMODE(rdir.stat().st_mode) == 0o700
    for f in rdir.iterdir():
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


# ── L3: cookie_secure and logout revocation ───────────────────────────────────────────


def _login(g, **kw):
    info = req("api/login", method="POST", **kw)
    assert g.check(info).allowed
    d = g.handle(info, json.dumps({"token": TOKEN}).encode())
    return d.headers["Set-Cookie"]


def test_l3_cookie_secure_setting():
    auto = gate(access="token", token=TOKEN, trusted_proxies=["10.0.0.1"])
    assert "; Secure" not in _login(auto)
    assert "; Secure" in _login(auto, ip="10.0.0.1", x_forwarded_proto="https")
    # X-Forwarded-Proto from an untrusted peer is ignored
    assert "; Secure" not in _login(auto, ip="10.0.0.2", x_forwarded_proto="https")
    assert "; Secure" in _login(gate(access="token", token=TOKEN, cookie_secure=True))
    off = gate(access="token", token=TOKEN, cookie_secure=False)
    assert "; Secure" not in _login(off, ip="127.0.0.1", host="localhost", x_forwarded_proto="x")
    env = resolve_settings({}, read_file=False, environ={"REDLINE_COOKIE_SECURE": "auto"})
    assert env.settings.cookie_secure == "auto"


def test_l3_logout_revokes_the_session():
    g = gate(access="token", token=TOKEN)
    value = _login(g).split(";", 1)[0].split("=", 1)[1]
    cookie = f"{SESSION_COOKIE}={value}"
    assert g.check(req(cookie=cookie)).allowed
    out = req("api/logout", method="POST", cookie=cookie)
    assert g.check(out).allowed
    assert g.handle(out, b"").status == 200
    assert g.check(req(cookie=cookie)).code == "login_required"  # a stolen copy is dead
    assert g.check(req(cookie=f"{SESSION_COOKIE}={_login(g).split(';')[0].split('=', 1)[1]}"))


# ── L5: api/config masks userinfo in base_url ─────────────────────────────────────────


def test_l5_config_masks_base_url_userinfo():
    assert mask_url_userinfo("http://u:pw@127.0.0.1:8000/x") == "http://***@127.0.0.1:8000/x"
    assert mask_url_userinfo("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    cfg = resolve_settings(
        {"base_url": "http://admin:hunter2@127.0.0.1:8000"}, read_file=False, environ={}
    )
    shown = json.dumps(cfg.public())
    assert "hunter2" not in shown and "admin" not in shown
    assert cfg.public()["settings"]["base_url"] == "http://***@127.0.0.1:8000"


# ── redline doctor ────────────────────────────────────────────────────────────────────


def test_doctor_warns_about_unset_access_and_shared_rate_limit(tmp_path):
    from redline_core.cli import _doctor_checks

    store = str(tmp_path / "r.db")
    unset = resolve_settings({"storage_path": store}, read_file=False, environ={})
    assert any(c[0] == "warn" and "access is not set" in c[2] for c in _doctor_checks(unset))
    sock = resolve_settings(
        {"storage_path": store, "access": "local", "trust_unix_socket": True},
        read_file=False,
        environ={},
    )
    assert any("trust_unix_socket" in c[2] for c in _doctor_checks(sock))
    token = resolve_settings(
        {"storage_path": store, "access": "token", "token": TOKEN}, read_file=False, environ={}
    )
    assert any(c[1] == "proxies" and "rate-limit" in c[2] for c in _doctor_checks(token))
