from __future__ import annotations

import gc
import weakref

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from fastapi_redline import install

from .conftest import ROOT


def _app(tmp_path, made, **kw):
    app = FastAPI()

    @app.get("/user/{uid}")
    def user(uid: int):
        engine = create_engine(f"sqlite:///{tmp_path / f'user{uid}.db'}")
        made.append(weakref.ref(engine))
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            conn.execute(text("SELECT 2"))
        engine.dispose()
        return {"ok": True}

    cfg = {
        "storage_path": str(tmp_path / "redline.db"),
        "project_root": str(ROOT),
        "access": "local",
    }
    return app, install(app, settings=cfg, **kw)


def _user_traces(redline):
    redline.store.flush(5)
    items = redline.store.list(None, limit=50).items
    return [redline.store.get(t["id"]) for t in items if t["path"].startswith("/user/")]


def test_per_request_engines_are_captured_with_distinct_aliases(tmp_path):
    app, redline = _app(tmp_path, [], engines="auto")
    with TestClient(app) as client:
        for uid in (1, 2, 3):
            assert client.get(f"/user/{uid}").status_code == 200
        traces = _user_traces(redline)
    assert len(traces) == 3
    aliases = set()
    for trace in traces:
        sql = [s for s in trace.spans if s.kind == "sql"]
        assert len(sql) == 2 and all(s.data["auto_engine"] for s in sql)
        aliases |= {s.data["db_alias"] for s in sql}
    assert len(aliases) == 3


def test_default_is_still_explicit(tmp_path):
    app, redline = _app(tmp_path, [])
    with TestClient(app) as client:
        client.get("/user/1")
        trace = _user_traces(redline)[0]
    assert not [s for s in trace.spans if s.kind == "sql"]


def test_disposed_engines_are_not_leaked(tmp_path):
    made: list = []
    app, _redline = _app(tmp_path, made, engines="auto")
    with TestClient(app) as client:
        for uid in range(1, 6):
            client.get(f"/user/{uid}")
    gc.collect()
    assert all(ref() is None for ref in made)


def test_invalid_engines_value_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        install(FastAPI(), engines="everything")
