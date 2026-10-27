from __future__ import annotations

import base64
import re
import secrets

import httpx
import pytest
from flask import abort, request

from flask_redline import loopback_guard

REMOTE = {"REMOTE_ADDR": "203.0.113.9"}


def basic(user="admin", password="pw"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_ui_index_and_assets(make_env):
    env = make_env()
    c = env.client()
    r = c.get("/_redline/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert b"Redline" in r.data and r.headers["cache-control"] == "no-store"
    html = r.get_data(as_text=True)
    asset = re.search(r'(?:src|href)="\./(assets/[^"]+\.js)"', html).group(1)
    a = c.get(f"/_redline/{asset}")
    assert a.status_code == 200
    assert a.headers["content-type"].startswith("text/javascript")
    assert "immutable" in a.headers["cache-control"]
    css = re.search(r'href="\./(assets/[^"]+\.css)"', html).group(1)
    assert c.get(f"/_redline/{css}").headers["content-type"].startswith("text/css")
    missing = c.get("/_redline/assets/nope.js")
    assert missing.status_code == 404 and missing.json["error"]["code"] == "not_found"
    assert c.get("/_redline", follow_redirects=False).status_code in (301, 307, 308)


def test_asset_traversal_and_security_headers(make_env):
    env = make_env()
    c = env.client()
    for path in (
        "/_redline/assets/..%2F..%2F__init__.py",
        "/_redline/assets/%2e%2e/%2e%2e/__init__.py",
        "/_redline/..%2F__init__.py",
        "/_redline/%2e%2e/%2e%2e/pyproject.toml",
    ):
        r = c.get(path)
        assert r.status_code == 404, path
        assert b"import" not in r.data  # never the source file
    page = c.get("/_redline/")
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"
    api = c.get("/_redline/api/meta")
    assert api.headers["x-frame-options"] == "DENY" and api.headers["cache-control"] == "no-store"
    # the application's own responses are left alone
    assert "x-frame-options" not in c.get("/orders/1").headers


def test_api_endpoints_roundtrip(make_env):
    env = make_env()
    c = env.client()
    c.get("/nplus1/items")
    c.get("/orders/1")
    env.redline.store.flush(5)
    meta = c.get("/_redline/api/meta").json
    assert meta["framework"] == "flask" and meta["storage"]["rows"] == 2
    listing = c.get("/_redline/api/traces?limit=5").json
    assert listing["total"] == 2
    n1 = next(i for i in listing["items"] if i["path"] == "/nplus1/items")
    detail = c.get(f"/_redline/api/traces/{n1['id']}").json
    assert detail["route"] == "/nplus1/items"
    assert any(i["kind"] == "n_plus_one" for i in detail["issues"])
    eps = c.get("/_redline/api/endpoints?window=1h").json["items"]
    assert {e["route"] for e in eps} == {"/nplus1/items", "/orders/<int:order_id>"}
    issues = c.get("/_redline/api/issues?kind=n_plus_one").json["items"]
    assert issues and issues[0]["suggestion"]["diff"]
    assert c.get("/_redline/api/traces?status=bogus").status_code == 400
    assert c.get("/_redline/api/nothing").status_code == 404
    assert c.get("/_redline/api/traces/zzz").json["error"]["code"] == "trace_not_found"
    # repeated query parameters reach the core as lists
    assert c.get("/_redline/api/traces?method=GET&method=POST").status_code == 200
    # delete
    assert c.delete(f"/_redline/api/traces/{n1['id']}").json == {"deleted": 1}
    assert c.delete("/_redline/api/traces").json == {"deleted": 1}


def test_api_error_shape_and_methods(make_env):
    env = make_env()
    c = env.client()
    assert c.put("/_redline/api/traces").json["error"]["code"] == "method_not_allowed"
    assert c.post("/_redline/api/explain", data="{").json["error"]["code"] == "invalid_json"
    assert c.get("/_redline/nope/deeper").json["error"]["code"] == "not_found"


# ── access control ────────────────────────────────────────────────────────────────────


def test_default_is_loopback_only(make_env):
    env = make_env()
    c = env.client()
    assert c.get("/_redline/api/meta").status_code == 200
    r = c.get("/_redline/api/meta", environ_overrides=REMOTE)
    assert r.status_code == 403
    assert r.json["error"]["code"] == "forbidden"
    assert "localhost" in r.json["error"]["message"]
    assert r.headers["x-frame-options"] == "DENY"
    assert c.get("/_redline/", environ_overrides=REMOTE).status_code == 403
    assert c.get("/_redline/assets/x.js", environ_overrides=REMOTE).status_code == 403
    assert c.get("/orders/1", environ_overrides=REMOTE).status_code == 200  # the app is not guarded


def test_loopback_client_addresses(make_env):
    env = make_env()
    c = env.client()
    for addr in ("127.0.0.1", "127.5.5.5", "::1", "::ffff:127.0.0.1"):
        r = c.get("/_redline/api/meta", environ_overrides={"REMOTE_ADDR": addr})
        assert r.status_code == 200, addr
    # "" (a unix socket) is not local unless trust_unix_socket is on
    for addr in ("203.0.113.9", "10.0.0.5", "2001:db8::1", "evil.com", ""):
        r = c.get("/_redline/api/meta", environ_overrides={"REMOTE_ADDR": addr})
        assert r.status_code == 403, addr


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:8000",
        "127.0.0.1",
        "127.0.0.1:9",
        "[::1]",
        "[::1]:8000",
        "app.localhost:3000",
        "LOCALHOST",
    ],
)
def test_loopback_host_names_are_accepted(make_env, host):
    env = make_env()
    assert env.client().get("/_redline/api/meta", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:8000", "testserver", "10.0.0.1", "localhost.evil.com"]
)
def test_dns_rebinding_is_blocked(make_env, host):
    """A rebinding page reaches 127.0.0.1 but its requests carry its own Host name."""
    env = make_env()
    c = env.client()
    for path in ("/_redline/api/meta", "/_redline/api/traces", "/_redline/"):
        r = c.get(path, headers={"Host": host})
        assert r.status_code == 403
        assert r.json["error"]["code"] == "forbidden"
        assert "Host" in r.json["error"]["message"]


def test_loopback_guard_allowed_hosts(make_env):
    env = make_env(permission=loopback_guard(allowed_hosts=["devbox"]))
    c = env.client()
    assert c.get("/_redline/api/meta", headers={"Host": "devbox:8000"}).status_code == 200
    assert c.get("/_redline/api/meta", headers={"Host": "localhost"}).status_code == 200
    assert c.get("/_redline/api/meta", headers={"Host": "evil.example"}).status_code == 403
    assert c.get("/_redline/api/meta", environ_overrides=REMOTE).status_code == 403


def test_warning_logged_without_permission(make_env, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="redline"):
        make_env()
    assert "with access='local'" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="redline"):
        make_env(permission=lambda request: True)
    assert "with access='local'" not in caplog.text


def test_permission_replaces_the_loopback_default(make_env):
    seen = []

    def permission(req):
        seen.append(req.path)
        return req.headers.get("X-Admin") == "yes"

    env = make_env(permission=permission)
    c = env.client()
    r = c.get("/_redline/api/meta", environ_overrides=REMOTE)  # not loopback, still refused
    assert r.status_code == 403 and r.json["error"]["code"] == "forbidden"
    ok = c.get("/_redline/api/meta", headers={"X-Admin": "yes"}, environ_overrides=REMOTE)
    assert ok.status_code == 200  # replaced: a remote client with the right header is fine
    ok = c.get("/_redline/api/meta", headers={"X-Admin": "yes", "Host": "evil.example"})
    assert ok.status_code == 200  # ... and the Host check is the permission's business now
    assert c.get("/_redline/", headers={"X-Admin": "yes"}).status_code == 200
    assert "/_redline/api/meta" in seen
    assert c.get("/orders/1").status_code == 200  # the app itself is not guarded


def test_permission_can_abort_with_a_status(make_env):
    security_user = "admin"

    def require_admin(req):
        auth = req.authorization
        if auth is None:
            abort(401)
        return secrets.compare_digest(auth.username or "", security_user) and (
            secrets.compare_digest(auth.password or "", "pw")
        )

    env = make_env(permission=require_admin)
    c = env.client()
    r = c.get("/_redline/api/meta")
    assert r.status_code == 401
    assert r.json["error"]["code"] == "unauthorized"
    assert c.get("/_redline/").status_code == 401
    assert c.get("/_redline/api/meta", headers=basic(password="bad")).status_code == 403
    assert c.get("/_redline/api/meta", headers=basic()).status_code == 200
    assert c.get("/_redline/", headers=basic()).status_code == 200
    # the Authorization header of *app* requests is redacted in the stored trace
    c.get("/orders/1", headers=basic())
    t = env.trace()
    assert t.request["headers"]["Authorization"] != basic()["Authorization"]


def test_permission_with_flask_login_style_proxy(make_env):
    """The permission sees the live request, so session/g based checks work."""

    def permission(req):
        return request.cookies.get("role") == "admin"

    env = make_env(permission=permission)
    c = env.client()
    assert c.get("/_redline/api/meta").status_code == 403
    c.set_cookie("role", "admin")
    assert c.get("/_redline/api/meta").status_code == 200


def test_csrf_blocks_cross_origin_mutations(make_env):
    env = make_env()
    c = env.client()
    c.get("/orders/1")
    env.redline.store.flush(5)
    evil = {"Origin": "https://evil.example"}
    assert c.delete("/_redline/api/traces", headers=evil).status_code == 403
    assert c.delete("/_redline/api/traces", headers={"Origin": "null"}).status_code == 403
    r = c.post("/_redline/api/explain", json={}, headers=evil)
    assert r.status_code == 403
    assert r.json["error"] == {"code": "csrf_failed", "message": "Cross-origin request blocked."}
    cross = c.delete("/_redline/api/traces", headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403 and cross.json["error"]["code"] == "csrf_failed"
    assert c.get("/_redline/api/traces", headers=evil).status_code == 200  # reads are safe
    # same-origin browser request and non-browser clients pass
    ok = c.post("/_redline/api/explain", json={}, headers={"Origin": "http://localhost"})
    assert ok.status_code == 400  # reached the API (invalid body), not blocked
    assert c.delete("/_redline/api/traces").status_code == 200


def test_csrf_is_checked_after_permission(make_env):
    env = make_env(permission=lambda request: False)
    r = env.client().delete("/_redline/api/traces", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json["error"]["code"] == "forbidden"


# ── EXPLAIN ───────────────────────────────────────────────────────────────────────────


def _explain(c, env, route):
    c.get(route)
    env.redline.store.flush(5)
    items = c.get("/_redline/api/traces").json["items"]
    tid = next(i["id"] for i in items if i["path"] == route)
    trace = c.get(f"/_redline/api/traces/{tid}").json
    span = next(s for s in trace["spans"] if s["kind"] == "sql")
    return c.post("/_redline/api/explain", json={"trace_id": tid, "span_id": span["id"]})


def test_explain(make_env):
    env = make_env()
    r = _explain(env.client(), env, "/orders/2")
    assert r.status_code == 200, r.text
    body = r.json
    assert body["vendor"] == "sqlite" and body["analyzed"] is False
    assert "orders" in body["text"].lower()
    assert body["tree"]["label"] or body["tree"]["children"]


def test_explain_reports_table_scan_findings(make_env):
    env = make_env()
    r = _explain(env.client(), env, "/scan")
    assert any("scan" in f["title"].lower() for f in r.json["findings"])


def test_explain_unknown_alias_is_501(make_env):
    env = make_env()
    c = env.client()
    c.get("/dup")
    env.redline.store.flush(5)
    tid = c.get("/_redline/api/traces").json["items"][0]["id"]
    trace = c.get(f"/_redline/api/traces/{tid}").json
    sid = next(s["id"] for s in trace["spans"] if s["kind"] == "sql")
    env.redline.engines.clear()
    r = c.post("/_redline/api/explain", json={"trace_id": tid, "span_id": sid})
    assert r.status_code == 501


def test_explain_analyze_is_refused_by_default(make_env):
    env = make_env()
    c = env.client()
    c.get("/orders/1")
    env.redline.store.flush(5)
    tid = c.get("/_redline/api/traces").json["items"][0]["id"]
    spans = c.get(f"/_redline/api/traces/{tid}").json["spans"]
    sid = next(s["id"] for s in spans if s["kind"] == "sql")
    r = c.post("/_redline/api/explain", json={"trace_id": tid, "span_id": sid, "analyze": True})
    assert r.status_code == 403 and r.json["error"]["code"] == "analyze_disabled"


# ── outbound HTTP (opt-in) ────────────────────────────────────────────────────────────


def test_httpx_spans(make_env):
    from flask_redline import instrument_httpx, uninstrument_httpx

    env = make_env()
    assert instrument_httpx() is True
    assert instrument_httpx() is True  # idempotent
    try:
        assert env.client().get("/http-out").status_code == 200
        t = env.trace("/http-out")
        outs = [s for s in t.spans if s.kind == "http_out"]
        assert len(outs) == 1
        out = outs[0]
        assert out.name == "GET inner/internal"
        assert out.data["status"] == 200 and out.data["method"] == "GET"
        assert "hunter2" not in out.data["url"] and "x=1" in out.data["url"]
        assert out.callsite and out.callsite.file == "tests/shop_app.py"
        assert env.trace("/internal") is not None  # the inner call is its own request
    finally:
        uninstrument_httpx()


def test_httpx_not_instrumented_by_default(make_env):
    env = make_env()
    assert env.client().get("/http-out").status_code == 200
    assert not [s for s in env.trace("/http-out").spans if s.kind == "http_out"]
    assert isinstance(httpx.__version__, str)


def test_requests_instrumentation_is_exported_and_idempotent():
    pytest.importorskip("requests")
    from flask_redline import instrument_requests, uninstrument_requests

    try:
        assert instrument_requests() is True
        assert instrument_requests() is True
    finally:
        uninstrument_requests()
