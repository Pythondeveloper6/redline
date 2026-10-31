from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from fastapi_redline.background import add_background_task
from redline_core.recorder import current


def _traces(env):
    store = env.store
    store.flush(5)
    items = store.list(None, limit=100).items
    return [store.get(item["id"]) for item in items]


def test_async_task_is_recorded_separately_with_parent_correlation(make_env):
    env = make_env()
    seen = []

    async def work(secret):
        seen.append(secret)
        return "result that must not be stored"

    @env.app.get("/schedule-async")
    async def schedule(background_tasks: BackgroundTasks):
        request_trace = current()
        add_background_task(background_tasks, work, "private argument")
        return {"request_trace": request_trace.id}

    with TestClient(env.app, base_url="http://localhost") as client:
        response = client.get("/schedule-async")

    assert seen == ["private argument"]
    request_id = response.json()["request_trace"]
    traces = _traces(env)
    request_trace = next(t for t in traces if t.id == request_id)
    task_trace = next(t for t in traces if t.method == "TASK")
    assert task_trace.id != request_trace.id
    assert task_trace.tags["parent_trace_id"] == request_trace.id
    assert task_trace.tags["job.name"].endswith("work")
    serialized = repr(task_trace.to_dict())
    assert "private argument" not in serialized
    assert "result that must not be stored" not in serialized


def test_sync_task_records_and_preserves_return_value(make_env):
    env = make_env()
    seen = []

    def work(value):
        seen.append(value)
        return value + 1

    @env.app.get("/schedule-sync")
    async def schedule(background_tasks: BackgroundTasks):
        add_background_task(background_tasks, work, 40)
        return {"ok": True}

    with TestClient(env.app, base_url="http://localhost") as client:
        assert client.get("/schedule-sync").json() == {"ok": True}

    assert seen == [40]
    task_trace = next(t for t in _traces(env) if t.method == "TASK")
    assert task_trace.tags["job.outcome"] == "succeeded"


def test_async_callable_instance_is_awaited(make_env):
    env = make_env()
    seen = []

    class Work:
        async def __call__(self, value):
            seen.append(value)

    @env.app.get("/schedule-callable")
    async def schedule(background_tasks: BackgroundTasks):
        add_background_task(background_tasks, Work(), "ran")
        return {"ok": True}

    with TestClient(env.app, base_url="http://localhost") as client:
        assert client.get("/schedule-callable").json() == {"ok": True}

    assert seen == ["ran"]
    task_trace = next(t for t in _traces(env) if t.method == "TASK")
    assert task_trace.tags["job.outcome"] == "succeeded"


def test_task_without_parent_trace_is_safe(make_env):
    env = make_env()
    tasks = BackgroundTasks()
    seen = []

    def work():
        seen.append("ran")

    add_background_task(tasks, work)
    asyncio.run(tasks())

    assert seen == ["ran"]
    assert current() is None
    task_trace = next(t for t in _traces(env) if t.method == "TASK")
    assert "parent_trace_id" not in task_trace.tags


def test_task_exception_is_recorded_and_re_raised(make_env):
    env = make_env(settings={"capture_error_details": True, "capture_locals": True})

    def fail(value):
        raise LookupError(f"failure for {value}")

    @env.app.get("/schedule-failure")
    async def schedule(background_tasks: BackgroundTasks):
        add_background_task(background_tasks, fail, "synthetic-private-secret")
        return {"scheduled": True}

    with TestClient(env.app, base_url="http://localhost") as client:
        with pytest.raises(LookupError, match="synthetic-private-secret"):
            client.get("/schedule-failure")

    task_trace = next(t for t in _traces(env) if t.method == "TASK")
    assert task_trace.tags["job.outcome"] == "failed"
    assert task_trace.error["type"] == "LookupError"
    assert task_trace.error["message"] == "[not captured]"
    assert task_trace.error["traceback"] == ""
    assert "frames" not in task_trace.error
    assert "synthetic-private-secret" not in repr(task_trace.to_dict())
