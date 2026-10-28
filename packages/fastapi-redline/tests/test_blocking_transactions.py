from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import redline_core.blocking as bl
from fastapi_redline import install
from redline_core.storage import SQLiteStore

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"))
    app = FastAPI()

    @app.get("/sleepy")
    async def sleepy():
        await asyncio.sleep(0.005)
        time.sleep(0.08)  # blocks the event loop
        await asyncio.sleep(0.005)
        return {"ok": True}

    @app.get("/sleepy-sync")
    def sleepy_sync():
        time.sleep(0.05)  # a plain def runs in the threadpool: fine
        return {"ok": True}

    @app.get("/fine")
    async def fine():
        await asyncio.sleep(0.03)
        return {"ok": True}

    @app.post("/write")
    async def write():
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO t (name) VALUES ('a')"))
            conn.execute(text("SELECT count(*) FROM t")).scalar()
        return {"ok": True}

    redline = install(
        app,
        engine=engine,
        settings={"storage_path": str(tmp_path / "r.db"), "project_root": str(ROOT)},
    )
    yield app, redline
    redline.close()
    engine.dispose()


def traces(redline):
    store = redline.store
    if getattr(store, "_closed", False):
        store = SQLiteStore(store.path, background=False)
    store.flush(5)
    return {t["path"]: store.get(t["id"]) for t in store.list(None, limit=50).items}


def kinds(trace, kind):
    return [s for s in trace.spans if s.kind == kind]


async def test_async_endpoint_with_time_sleep_is_blocking_in_async(env):
    app, redline = env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/sleepy")).status_code == 200
        assert (await c.get("/fine")).status_code == 200
    by_path = traces(redline)
    trace = by_path["/sleepy"]
    # A busy host can produce additional lag spans; verify the deliberately blocked call.
    span = next(s for s in kinds(trace, "blocking") if s.data.get("call") == "time.sleep")
    assert span.data["call"] == "time.sleep" and span.data["reason"] == "loop_lag"
    assert span.callsite.file == "tests/test_blocking_transactions.py"
    issue = next(i for i in trace.issues if i.kind == "blocking_in_async" and span.id in i.span_ids)
    assert issue.title.startswith("Event loop blocked ")
    assert issue.title.endswith(
        f"by time.sleep at tests/test_blocking_transactions.py:{span.callsite.line}"
    )
    assert "asyncio.sleep" in issue.suggestion.summary
    assert not any(s.data.get("call") == "time.sleep" for s in kinds(by_path["/fine"], "blocking"))
    assert not bl._active and not bl._loop_threads


def test_testclient_and_threadpool_endpoints(env):
    app, redline = env
    with TestClient(app) as client:
        assert client.get("/sleepy").status_code == 200
        assert client.get("/sleepy-sync").status_code == 200
    by_path = traces(redline)
    assert len(kinds(by_path["/sleepy"], "blocking")) == 1
    assert kinds(by_path["/sleepy-sync"], "blocking") == []
    assert not bl._active


def test_transaction_span_from_the_app(env):
    app, redline = env
    with TestClient(app) as client:
        assert client.post("/write").status_code == 200
    trace = traces(redline)["/write"]
    [tx] = kinds(trace, "transaction")
    assert tx.data == {"db_alias": "default", "outcome": "commit", "queries": 2, "http_out": 0}
    assert all(s.data["in_transaction"] for s in kinds(trace, "sql"))


def test_detect_blocking_off(tmp_path):
    app = FastAPI()

    @app.get("/sleepy")
    async def sleepy():
        time.sleep(0.05)
        return {}

    redline = install(
        app,
        settings={
            "storage_path": str(tmp_path / "r.db"),
            "project_root": str(ROOT),
            "detect_blocking": False,
        },
    )
    with TestClient(app) as client:
        client.get("/sleepy")
    assert kinds(traces(redline)["/sleepy"], "blocking") == []
    redline.close()
