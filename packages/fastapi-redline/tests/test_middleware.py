from __future__ import annotations

import asyncio
import json

import httpx
from fastapi.testclient import TestClient


def aclient(env):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://t")


async def test_async_endpoint_trace(make_env):
    env = make_env()
    async with aclient(env) as c:
        r = await c.get("/orders/3?x=1", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    t = env.trace()
    assert t.framework == "fastapi"
    assert t.method == "GET" and t.path == "/orders/3"
    assert t.route == "/orders/{order_id}"
    assert t.view.endswith("shop_app.get_order")
    assert t.status == 200
    assert t.query_count == 1
    sql = next(s for s in t.spans if s.kind == "sql")
    assert sql.data["vendor"] == "sqlite" and sql.data["db_alias"] == "default"
    assert sql.callsite and sql.callsite.file == "tests/shop_app.py"  # greenlet bridge
    assert "select(Order)" in sql.callsite.code
    assert t.request["headers"]["authorization"] != "Bearer s3cret"
    assert "s3cret" not in json.dumps(t.request)
    assert t.request["query"] == {"x": "1"}
    assert json.loads(t.response["body"]) == {"id": 3, "note": "n2"}


async def test_sync_endpoint_threadpool_attaches_to_right_trace(make_env):
    env = make_env()
    async with aclient(env) as c:
        await asyncio.gather(*[c.get("/sync-count") for _ in range(6)], c.get("/orders/1"))
    traces = [env.store.get(t["id"]) for t in env.traces()]
    assert len(traces) == 7
    by_route = {}
    for t in traces:
        by_route.setdefault(t.route, []).append(t)
    assert len(by_route["/sync-count"]) == 6
    for t in by_route["/sync-count"]:
        assert t.query_count == 1, "each sync request owns exactly its own query"
        assert [s.data["db_alias"] for s in t.spans if s.kind == "sql"] == ["sync"]
    assert by_route["/orders/{order_id}"][0].query_count == 1


async def test_concurrent_requests_are_isolated(make_env):
    env = make_env()
    async with aclient(env) as c:
        rs = await asyncio.gather(*[c.get(f"/slow/{i}") for i in range(1, 9)])
    assert all(r.status_code == 200 for r in rs)
    traces = [env.store.get(t["id"]) for t in env.traces()]
    assert len(traces) == 8
    for t in traces:
        n = int(t.path.rsplit("/", 1)[1])
        params = [s.data["params"] for s in t.spans if s.kind == "sql"]
        assert params == [[n], [n]], (t.path, params)
        assert t.duration_ms >= 45


def test_body_replay_and_capture(make_env):
    env = make_env()
    payload = {"user": "ann", "password": "hunter2"}
    with TestClient(env.app) as c:
        r = c.post("/echo", json=payload)
    assert r.json()["body"] == json.dumps(payload, separators=(",", ":"))  # app saw it intact
    t = env.trace()
    body = json.loads(t.request["body"])
    assert body["user"] == "ann" and body["password"] != "hunter2"
    assert t.request["body_truncated"] is False


def test_large_request_body_is_capped_and_masked(make_env):
    env = make_env(settings={"max_body_bytes": 200})
    big = json.dumps({"password": "hunter2", "pad": "x" * 5000})
    with TestClient(env.app) as c:
        r = c.post("/echo", content=big, headers={"content-type": "application/json"})
    assert r.json()["len"] == len(big)  # the app got everything
    t = env.trace()
    assert t.request["body_truncated"] is True
    assert "hunter2" not in t.request["body"]
    assert len(t.request["body"]) <= 200


def test_streaming_response_is_capped_not_buffered(make_env):
    env = make_env(settings={"max_body_bytes": 500})
    with TestClient(env.app) as c:
        r = c.get("/stream")
    assert r.text.count("\n") == 200
    t = env.trace()
    assert t.response["body_truncated"] is True
    assert len(t.response["body"]) <= 500
    assert t.response["size"] == len(r.content)


def test_unhandled_exception_recorded_and_reraised(make_env):
    env = make_env()
    with TestClient(env.app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500
    t = env.trace()
    assert t.status == 500
    assert t.error["type"] == "RuntimeError" and t.error["message"] == "kaboom"
    assert "kaboom" in t.error["traceback"]
    assert t.route == "/boom"
    assert t.tags["sampled_by"] in {"all", "error"}

    with TestClient(env.app) as c:  # default: server exceptions propagate to the caller
        try:
            c.get("/boom")
        except RuntimeError as exc:
            assert str(exc) == "kaboom"
        else:  # pragma: no cover
            raise AssertionError("exception was swallowed")


def test_404_and_validation_are_recorded(make_env):
    env = make_env()
    with TestClient(env.app) as c:
        assert c.get("/nope").status_code == 404
        assert c.get("/orders/abc").status_code == 422
    statuses = sorted(t["status"] for t in env.traces())
    assert statuses == [404, 422]


def test_redline_prefix_is_ignored(make_env):
    env = make_env()
    with TestClient(env.app) as c:
        assert c.get("/_redline/").status_code == 200
        assert c.get("/_redline/api/meta").status_code == 200
        assert c.get("/_redline/api/traces").status_code == 200
        c.get("/dup")
    paths = [t["path"] for t in env.traces()]
    assert paths == ["/dup"]


def test_sampling_rate_zero_keeps_nothing_but_errors(make_env):
    env = make_env(settings={"sample_rate": 0.0})
    with TestClient(env.app, raise_server_exceptions=False) as c:
        c.get("/dup")
        c.get("/boom")
    assert [t["path"] for t in env.traces()] == ["/boom"]


def test_disabled_records_nothing(make_env):
    env = make_env(settings={"enabled": False})
    with TestClient(env.app) as c:
        assert c.get("/dup").status_code == 200
    assert env.traces() == []


def test_run_header_becomes_tags(make_env):
    env = make_env()
    with TestClient(env.app) as c:
        c.get("/dup", headers={"X-Redline-Run": "run42;step=3"})
    t = env.trace()
    assert t.tags["run_id"] == "run42" and t.tags["step"] == "3"


def test_profile_opt_in_query_and_header(make_env):
    env = make_env()
    with TestClient(env.app) as c:
        c.get("/orders/1")
        c.get("/orders/1?_redline=profile")
        c.get("/orders/2", headers={"X-Redline-Profile": "1"})
    items = {(t["path"], t["has_profile"]) for t in env.traces()}
    assert items == {("/orders/1", False), ("/orders/1", True), ("/orders/2", True)}
    prof_id = next(t["id"] for t in env.traces() if t["has_profile"])
    profile = env.store.get_profile(prof_id)
    assert profile["$schema"].startswith("https://www.speedscope.app")
    assert profile["profiles"]


def test_profile_off_mode_ignores_request(make_env):
    env = make_env(settings={"profile": "off"})
    with TestClient(env.app) as c:
        c.get("/orders/1?_redline=profile")
    assert not env.traces()[0]["has_profile"]


def test_existing_lifespan_is_preserved_and_store_closed(make_env):
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from fastapi_redline import install

    events = []

    @asynccontextmanager
    async def lifespan(app):
        events.append("start")
        yield {"k": 1}
        events.append("stop")

    app = FastAPI(lifespan=lifespan)
    rl = install(app, settings={"storage_path": ":memory:"}, dependencies=[])
    with TestClient(app):
        assert events == ["start"]
    assert events == ["start", "stop"]
    assert rl.store.dropped == 0


def test_double_install_rejected(make_env):
    import pytest

    from fastapi_redline import install

    env = make_env()
    with pytest.raises(RuntimeError):
        install(env.app, settings={"storage_path": ":memory:"})


def test_mounted_subapp_route_template(make_env):
    from fastapi import FastAPI

    env = make_env()
    sub = FastAPI()

    @sub.get("/things/{tid}")
    async def thing(tid: int):
        return {"tid": tid}

    env.app.mount("/api/v1", sub)
    with TestClient(env.app) as c:
        assert c.get("/api/v1/things/9").status_code == 200
    t = env.trace()
    assert t.path == "/api/v1/things/9"
    assert t.route in ("/api/v1/things/{tid}", "/things/{tid}")
