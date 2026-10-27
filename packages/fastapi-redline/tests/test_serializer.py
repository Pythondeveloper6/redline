"""Response serialization is timed as ``serializer`` spans and flagged when slow."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_redline import install
from fastapi_redline._serialize import uninstrument_serialization
from redline_core.storage import SQLiteStore

ROOT = Path(__file__).resolve().parent.parent


class Item(BaseModel):
    id: int
    name: str
    tags: list[str] = []


def make_app(tmp_path, **settings):
    app = FastAPI()

    @app.get("/items", response_model=list[Item])
    def items():
        return [{"id": i, "name": f"n{i}", "tags": ["a", "b"]} for i in range(400)]

    @app.get("/item", response_model=Item)
    async def one():
        return {"id": 1, "name": "x"}

    @app.get("/plain")
    async def plain():
        return {"ok": True}

    cfg = {
        "storage_path": str(tmp_path / "r.db"),
        "project_root": str(ROOT),
        "sample_rate": 1.0,
        **settings,
    }
    redline = install(app, settings=cfg, dependencies=[])
    return app, redline


@pytest.fixture(autouse=True)
def _restore():
    yield
    uninstrument_serialization()


def trace_of(redline, app, path):
    with TestClient(app) as c:
        assert c.get(path).status_code == 200
    store = SQLiteStore(redline.store.path, background=False)  # the app's store closed on exit
    item = next(t for t in store.list(None, limit=50).items if t["path"] == path)
    return store.get(item["id"])


def serializer_spans(trace):
    return [s for s in trace.spans if s.kind == "serializer"]


def test_list_response_model_span(tmp_path):
    app, redline = make_app(tmp_path)
    (span,) = serializer_spans(trace_of(redline, app, "/items"))
    assert span.data == {
        "library": "pydantic",
        "serializer": "Item",
        "items": 400,
        "many": True,
    }
    assert span.duration_ms > 0


def test_single_model_and_plain_dict(tmp_path):
    app, redline = make_app(tmp_path)
    (one,) = serializer_spans(trace_of(redline, app, "/item"))
    assert one.data["serializer"] == "Item" and one.data["many"] is False
    assert one.data["items"] is None


def test_plain_dict_without_response_model(tmp_path):
    app, redline = make_app(tmp_path)
    (plain,) = serializer_spans(trace_of(redline, app, "/plain"))
    assert plain.data["serializer"] == "dict"


def test_slow_serializer_issue(tmp_path):
    app, redline = make_app(tmp_path, slow_serializer_ms=0.001)
    trace = trace_of(redline, app, "/items")
    issue = next(i for i in trace.issues if i.kind == "slow_serializer")
    assert issue.extra["serializer"] == "Item" and issue.extra["items"] == 400
    assert "Item" in issue.title and "400 items" in issue.title
    assert issue.suggestion is not None


def test_fast_serializer_span_skips_the_stack_walk(tmp_path, monkeypatch):
    """The callsite (a stack walk, the costliest part of the span) is only taken when the
    span is slow enough to become an issue."""
    from redline_core.callsite import CallsiteCapturer

    calls = []
    original = CallsiteCapturer.capture

    def counting(self, *a, **kw):
        calls.append(1)
        return original(self, *a, **kw)

    monkeypatch.setattr(CallsiteCapturer, "capture", counting)
    app, redline = make_app(tmp_path)
    (span,) = serializer_spans(trace_of(redline, app, "/plain"))
    assert span.callsite is None and calls == []
