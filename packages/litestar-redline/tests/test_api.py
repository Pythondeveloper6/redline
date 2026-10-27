from __future__ import annotations

import base64
import secrets

import httpx
import pytest
from litestar import Litestar, get
from litestar.exceptions import NotAuthorizedException

from .conftest import allow_all, client


def basic(user="admin", password="pw"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_ui_index_and_assets(make_env):
    env = make_env()
    with client(env) as c:
        r = c.get("/_redline/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "Redline" in r.text and r.headers["cache-control"] == "no-store"
        import re

        asset = re.search(r'(?:src|href)="\./(assets/[^"]+\.js)"', r.text).group(1)
        a = c.get(f"/_redline/{asset}")
        assert a.status_code == 200
        assert a.headers["content-type"].startswith("text/javascript")
        assert "immutable" in a.headers["cache-control"]
        css = re.search(r'href="\./(assets/[^"]+\.css)"', r.text).group(1)
        assert c.get(f"/_redline/{css}").headers["content-type"].startswith("text/css")
        assert c.get("/_redline/assets/nope.js").status_code == 404
        assert c.get("/_redline/assets/..%2f..%2findex.html").status_code in (404, 200)
        assert c.get("/_redline", follow_redirects=False).status_code in (307, 308)


def test_asset_traversal_blocked(make_env):
    env = make_env()
    with client(env) as c:
        r = c.get("/_redline/assets/%2e%2e/%2e%2e/__init__.py")
        assert r.status_code == 404
        assert "def " not in r.text


def test_api_endpoints_roundtrip(make_env):
    env = make_env()
    with client(env) as c:
        c.get("/nplus1/items")
        c.get("/orders/1")
        env.redline.store.flush(5)
        meta = c.get("/_redline/api/meta").json()
        assert meta["framework"] == "litestar" and meta["storage"]["rows"] == 2
        listing = c.get("/_redline/api/traces?limit=5").json()
        assert listing["total"] == 2
        n1 = next(i for i in listing["items"] if i["path"] == "/nplus1/items")
        detail = c.get(f"/_redline/api/traces/{n1['id']}").json()
        assert detail["route"] == "/nplus1/items"
        assert any(i["kind"] == "n_plus_one" for i in detail["issues"])
        eps = c.get("/_redline/api/endpoints?window=1h").json()["items"]
        assert {e["route"] for e in eps} == {"/nplus1/items", "/orders/{order_id}"}
        issues = c.get("/_redline/api/issues?kind=n_plus_one").json()["items"]
        assert issues and issues[0]["suggestion"]["diff"]
        assert c.get("/_redline/api/traces?status=bogus").status_code == 400
        assert c.get("/_redline/api/nothing").status_code == 404
        assert c.get("/_redline/api/traces/zzz").json()["error"]["code"] == "trace_not_found"
        # delete
        assert c.delete(f"/_redline/api/traces/{n1['id']}").json() == {"deleted": 1}
        assert c.delete("/_redline/api/traces").json() == {"deleted": 1}


def test_default_is_loopback_only(make_env):
    env = make_env(guards=None)
    assert env.redline.api  # installed with the default guard
    # TestClient reports the client as "testclient" (loopback in tests)
    with client(env, base_url="http://localhost:8000") as c:
        assert c.get("/_redline/api/meta").status_code == 200
    from litestar_redline._security import _is_loopback

    assert [_is_loopback(h) for h in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")] == [
        True
    ] * 4
    assert not any(_is_loopback(h) for h in ("203.0.113.9", "10.0.0.5", "2001:db8::1", "evil.com"))
    with client(env, remote=("203.0.113.9", 5555), base_url="http://localhost") as c:
        r = c.get("/_redline/api/meta")
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"
        assert "localhost" in r.json()["error"]["message"]
        assert c.get("/_redline/").status_code == 403
        assert c.get("/orders/1").status_code == 200  # the app itself is not guarded


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
    env = make_env(guards=None)
    with client(env) as c:
        assert c.get("/_redline/api/meta", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:8000", "testserver", "10.0.0.1", "localhost.evil.com"]
)
def test_dns_rebinding_is_blocked(make_env, host):
    """A rebinding page reaches 127.0.0.1 but its requests carry its own Host name."""
    env = make_env(guards=None)
    with client(env) as c:
        for path in ("/_redline/api/meta", "/_redline/api/traces", "/_redline/"):
            r = c.get(path, headers={"Host": host})
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "forbidden"
            assert "Host" in r.json()["error"]["message"]


def test_loopback_guard_allowed_hosts(make_env):
    from litestar_redline import loopback_guard

    env = make_env(guards=[loopback_guard(allowed_hosts=["devbox"])])
    with client(env) as c:
        assert c.get("/_redline/api/meta", headers={"Host": "devbox:8000"}).status_code == 200
        assert c.get("/_redline/api/meta", headers={"Host": "localhost"}).status_code == 200
        assert c.get("/_redline/api/meta", headers={"Host": "evil.example"}).status_code == 403


def test_warning_logged_without_guards(make_env, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="redline"):
        make_env(guards=None)
    assert "with access='local'" in caplog.text


def test_basic_auth_guard(make_env):
    def require_admin(connection, _handler) -> None:
        auth = connection.headers.get("authorization", "")
        try:
            decoded = base64.b64decode(auth.removeprefix("Basic ")).decode()
            user, _, password = decoded.partition(":")
        except Exception:
            user = password = ""
        ok = secrets.compare_digest(user, "admin") and secrets.compare_digest(password, "pw")
        if not ok:
            raise NotAuthorizedException("Not authenticated", headers={"WWW-Authenticate": "Basic"})

    env = make_env(guards=[require_admin])
    with client(env, remote=("203.0.113.9", 1)) as c:
        r = c.get("/_redline/api/meta")
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Basic"
        assert r.json() == {"error": {"code": "unauthorized", "message": "Not authenticated"}}
        assert r.headers["x-frame-options"] == "DENY"
        bad = c.get("/_redline/api/meta", headers=basic(password="bad"))
        assert bad.json()["error"]["code"] == "unauthorized"
        assert c.get("/_redline/").status_code == 401
        assert c.get("/_redline/api/meta", headers=basic(password="bad")).status_code == 401
        assert c.get("/_redline/api/meta", headers=basic()).status_code == 200
        assert c.get("/_redline/", headers=basic()).status_code == 200
        # authorization header of *app* requests is redacted in the stored trace
        c.get("/orders/1", headers=basic())
    t = env.trace()
    assert t.request["headers"]["authorization"] != basic()["Authorization"]


def test_guards_replace_the_loopback_default(make_env):
    calls = []

    def guard(connection, _handler):
        calls.append(1)

    env = make_env(guards=[guard])
    with client(env, remote=("203.0.113.9", 1), base_url="http://evil.example") as c:
        # neither the remote client nor the foreign Host is refused: the guard replaced local_only
        assert c.get("/_redline/api/meta").status_code == 200
    assert calls


def test_async_guard_and_plain_403(make_env):
    async def deny(connection, _handler):
        from litestar.exceptions import PermissionDeniedException

        raise PermissionDeniedException("nope")

    env = make_env(guards=[deny])
    with client(env) as c:
        r = c.get("/_redline/api/traces")
        assert r.status_code == 403
        assert r.json() == {"error": {"code": "forbidden", "message": "nope"}}
        assert c.get("/dup").status_code == 200


def test_csrf_blocks_cross_origin_mutations(make_env):
    env = make_env()
    with client(env, base_url="http://app.test") as c:
        c.get("/orders/1")
        env.redline.store.flush(5)
        evil = {"Origin": "https://evil.example"}
        assert c.delete("/_redline/api/traces", headers=evil).status_code == 403
        assert c.delete("/_redline/api/traces", headers={"Origin": "null"}).status_code == 403
        r = c.post("/_redline/api/explain", json={}, headers=evil)
        assert r.status_code == 403
        assert r.json()["error"] == {
            "code": "csrf_failed",
            "message": "Cross-origin request blocked.",
        }
        assert c.put("/_redline/api/traces").json()["error"]["code"] == "method_not_allowed"
        assert (
            c.delete("/_redline/api/traces", headers={"Sec-Fetch-Site": "cross-site"}).status_code
            == 403
        )
        assert c.get("/_redline/api/traces", headers=evil).status_code == 200  # reads are safe
        # same-origin browser request and non-browser clients pass
        ok = c.post("/_redline/api/explain", json={}, headers={"Origin": "http://app.test"})
        assert ok.status_code == 400  # reached the API (invalid body), not blocked
        assert c.delete("/_redline/api/traces").status_code == 200


# ── EXPLAIN ───────────────────────────────────────────────────────────────────────────


def _explain(c, env, route, alias):
    c.get(route)
    env.redline.store.flush(5)
    items = c.get("/_redline/api/traces").json()["items"]
    tid = next(i["id"] for i in items if i["path"] == route)
    trace = c.get(f"/_redline/api/traces/{tid}").json()
    span = next(s for s in trace["spans"] if s["kind"] == "sql" and s["data"]["db_alias"] == alias)
    return c.post("/_redline/api/explain", json={"trace_id": tid, "span_id": span["id"]})


def test_explain_async_engine(make_env):
    env = make_env()
    with client(env) as c:
        r = _explain(c, env, "/orders/2", "default")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["vendor"] == "sqlite" and body["analyzed"] is False
        assert "orders" in body["text"].lower()
        assert body["tree"]["label"] or body["tree"]["children"]


def test_explain_sync_engine_other_alias(make_env):
    env = make_env()
    with client(env) as c:
        r = _explain(c, env, "/sync-count", "sync")
        assert r.status_code == 200, r.text
        assert "tags" in r.json()["text"].lower()


def test_explain_reports_table_scan_findings(make_env):
    env = make_env()
    with client(env) as c:
        r = _explain(c, env, "/sync-count", "sync")
    assert any("scan" in f["title"].lower() for f in r.json()["findings"])


def test_explain_unknown_alias_is_501(make_env):
    env = make_env(engine_mode="sync")
    with client(env) as c:
        c.get("/sync-count")
        env.redline.store.flush(5)
        tid = c.get("/_redline/api/traces").json()["items"][0]["id"]
        trace = c.get(f"/_redline/api/traces/{tid}").json()
        sid = next(s["id"] for s in trace["spans"] if s["kind"] == "sql")
        env.redline.engines.clear()
        r = c.post("/_redline/api/explain", json={"trace_id": tid, "span_id": sid})
        assert r.status_code == 501


# ── httpx ─────────────────────────────────────────────────────────────────────────────


async def test_httpx_spans(make_env):
    from litestar_redline import instrument_httpx, uninstrument_httpx

    env = make_env()
    assert instrument_httpx() is True
    assert instrument_httpx() is True  # idempotent
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=env.app), base_url="http://t"
        ) as c:
            assert (await c.get("/http-out")).status_code == 200
        t = env.trace("/http-out")
        outs = [s for s in t.spans if s.kind == "http_out"]
        assert len(outs) == 1
        out = outs[0]
        assert out.name == "GET inner/internal"
        assert out.data["status"] == 200 and out.data["method"] == "GET"
        assert "hunter2" not in out.data["url"] and "x=1" in out.data["url"]
        assert out.callsite and out.callsite.file == "tests/shop_app.py"
        # the internal call is its own request, recorded separately
        assert env.trace("/internal") is not None
    finally:
        uninstrument_httpx()


async def test_httpx_not_instrumented_by_default(make_env):
    env = make_env()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://t"
    ) as c:
        await c.get("/http-out")
    assert not [s for s in env.trace("/http-out").spans if s.kind == "http_out"]


def test_disabled_installs_no_listeners_and_no_middleware(tmp_path):
    from sqlalchemy import create_engine, event, text

    from litestar_redline import RedlineMiddleware, RedlinePlugin

    engine = create_engine(f"sqlite:///{tmp_path / 'd.db'}")

    @get("/q", sync_to_thread=True)
    def q() -> dict:
        with engine.connect() as conn:
            return {"v": conn.execute(text("SELECT 1")).scalar()}

    plugin = RedlinePlugin(
        engine=engine,
        settings={"enabled": False, "storage_path": str(tmp_path / "r.db")},
        guards=[allow_all],
    )
    app = Litestar(route_handlers=[q], plugins=[plugin])
    try:
        assert not event.contains(engine, "before_cursor_execute", _any_listener(engine))
        assert not any(getattr(m, "middleware", None) is RedlineMiddleware for m in app.middleware)
        with client_for(app) as c:
            assert c.get("/q").json() == {"v": 1}
            assert c.get("/_redline/api/meta").json()["storage"]["rows"] == 0
        assert "default" in plugin.engines  # EXPLAIN still knows the engine
    finally:
        plugin.close()


def client_for(app):
    from litestar.testing import TestClient

    return TestClient(app, base_url="http://localhost")


def _any_listener(engine):
    """The registered before_cursor_execute listener, if any (else a dummy)."""
    for fn in engine.dispatch.before_cursor_execute:
        return fn
    return lambda *a: None


def test_asset_traversal_and_security_headers(make_env):
    env = make_env()
    with client(env) as c:
        for path in (
            "/_redline/assets/..%2F..%2F__init__.py",
            "/_redline/assets/%2e%2e/%2e%2e/__init__.py",
            "/_redline/..%2F__init__.py",
        ):
            r = c.get(path)
            assert r.status_code == 404, path
            assert "import" not in r.text  # never the source file
        page = c.get("/_redline/")
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["x-content-type-options"] == "nosniff"
