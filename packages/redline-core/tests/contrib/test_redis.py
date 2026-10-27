from __future__ import annotations

import os

import fakeredis
import fakeredis.aioredis
import pytest
import redis

from redline_core.contrib.redis import instrument_redis, uninstrument_redis


@pytest.fixture(autouse=True)
def patched():
    assert instrument_redis() and instrument_redis()
    yield
    uninstrument_redis()


def test_sync_commands(tracer):
    r = fakeredis.FakeRedis()
    b = tracer.start()
    r.set("user:1", "x")
    r.get("user:1")
    r.get("user:2")
    r.mget(["user:1", "user:2"])
    r.delete(b"user:1")
    spans = tracer.finish(b)
    got = [(s.data["op"], s.data["key"], s.data["hit"]) for s in spans]
    assert got == [
        ("set", "user:1", None),
        ("get", "user:1", True),
        ("get", "user:2", False),
        ("mget", "user:1", True),
        ("del", "user:1", None),
    ]
    assert all(s.kind == "cache" and s.data["backend"] == "redis" for s in spans)
    assert spans[1].name == "get user:1"
    assert spans[1].callsite is not None


def test_key_is_capped(tracer):
    r = fakeredis.FakeRedis()
    b = tracer.start()
    r.get("k" * 500)
    (span,) = tracer.finish(b)
    assert len(span.data["key"]) == 200


def test_credential_commands_have_no_key(tracer):
    r = fakeredis.FakeRedis()
    b = tracer.start()
    for args in (
        ("AUTH", "hunter2"),
        ("AUTH", "bob", "hunter2"),
        ("HELLO", "3", "AUTH", "bob", "hunter2"),
        ("CONFIG SET", "requirepass", "hunter2"),
        ("CONFIG", "SET", "requirepass", "hunter2"),
        ("MIGRATE", "hunter2", 6379, "", 0, 5000, "AUTH", "hunter2", "KEYS", "a"),
        ("ACL SETUSER", "hunter2", ">hunter2"),
    ):
        try:
            r.execute_command(*args)
        except redis.RedisError:
            pass
    spans = tracer.finish(b)
    assert len(spans) == 7
    for span in spans:
        assert span.data["key"] is None
        assert "hunter2" not in span.name and "hunter2" not in str(span.data)
    assert spans[0].name == "auth"


def test_sync_pipeline(tracer):
    r = fakeredis.FakeRedis()
    b = tracer.start()
    with r.pipeline() as p:
        p.set("a", 1)
        p.set("b", 2)
        p.get("a")
        p.execute()
    (span,) = tracer.finish(b)
    assert span.data["op"] == "pipeline"
    assert span.data["commands"] == 3


async def test_async_commands_and_pipeline(tracer):
    r = fakeredis.aioredis.FakeRedis()
    b = tracer.start()
    await r.set("k", "v")
    assert await r.get("k") == b"v"
    async with r.pipeline() as p:
        p.get("k")
        p.get("z")
        await p.execute()
    spans = tracer.finish(b)
    assert [s.data["op"] for s in spans] == ["set", "get", "pipeline"]
    assert spans[1].data["hit"] is True
    assert spans[2].data["commands"] == 2
    await r.aclose()


def test_no_trace_noop():
    r = fakeredis.FakeRedis()
    r.set("a", 1)
    assert r.get("a") == b"1"


@pytest.mark.skipif(not os.environ.get("REDLINE_TEST_REDIS_URL"), reason="needs a redis server")
def test_real_redis(tracer):
    r = redis.Redis.from_url(os.environ["REDLINE_TEST_REDIS_URL"])
    b = tracer.start()
    r.get("redline-test-missing")
    (span,) = tracer.finish(b)
    assert span.data["hit"] is False
