"""FastAPI/Starlette background tasks are recorded as jobs linked to the request trace."""

import pytest

from redline_core.contrib.starlette_background import (
    instrument_background_tasks,
    uninstrument_background_tasks,
)

pytest.importorskip("fastapi_redline")


def test_background_task_becomes_job_linked_to_request(store):
    from fastapi import BackgroundTasks, FastAPI
    from fastapi.testclient import TestClient

    from fastapi_redline import install

    app = FastAPI()
    install(app, store=store, settings={"access": "local", "capture_locals": False})
    ran = []

    def send_email(address):
        ran.append(address)

    def broken():
        raise RuntimeError("example failure")

    @app.post("/signup")
    def signup(tasks: BackgroundTasks):
        tasks.add_task(send_email, "user@example.test")
        tasks.add_task(broken)
        return {"ok": True}

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.post("/signup").status_code == 200
            assert ran == ["user@example.test"]
            store.flush()
            traces = [store.get(i["id"]) for i in store.list(limit=20).items]
            jobs = [t for t in traces if t.method == "TASK"]
            requests = [t for t in traces if t.route == "/signup"]
            assert len(requests) == 1 and len(jobs) == 2
            by_name = {t.tags["job.name"].rsplit(".", 1)[-1]: t for t in jobs}
            assert by_name["send_email"].tags["job.outcome"] == "succeeded"
            assert by_name["broken"].tags["job.outcome"] == "failed"
            for task in jobs:
                assert task.tags["parent_trace_id"] == requests[0].id
                assert task.tags["job.queue"] == "starlette.background"
    finally:
        uninstrument_background_tasks()


def test_instrument_is_idempotent_and_reversible():
    from starlette.background import BackgroundTask

    original = BackgroundTask.__call__
    assert instrument_background_tasks() and instrument_background_tasks()
    assert BackgroundTask.__call__ is not original
    uninstrument_background_tasks()
    assert BackgroundTask.__call__ is original
