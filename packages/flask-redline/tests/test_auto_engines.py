from __future__ import annotations

import gc
import weakref

import pytest
from flask import Flask
from sqlalchemy import create_engine, text

from flask_redline import Redline

from .conftest import ROOT, BufferedClient


def _app(tmp_path, engines_made, **kw):
    app = Flask(__name__)

    @app.get("/user/<int:uid>")
    def user(uid: int):
        # a per-user engine, as with per-user token auth
        engine = create_engine(f"sqlite:///{tmp_path / f'user{uid}.db'}")
        engines_made.append(weakref.ref(engine))
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            conn.execute(text("SELECT 2"))
        engine.dispose()
        return "ok"

    cfg = {
        "storage_path": str(tmp_path / "redline.db"),
        "project_root": str(ROOT),
        "access": "local",
    }
    app.test_client_class = BufferedClient
    redline = Redline(app, settings=cfg, **kw)
    return app, redline


def _traces(redline):
    redline.store.flush(5)
    return [redline.store.get(t["id"]) for t in redline.store.list(None, limit=50).items]


def test_per_request_engines_are_captured_with_distinct_aliases(tmp_path):
    made: list = []
    app, redline = _app(tmp_path, made, engines="auto")
    try:
        client = app.test_client()
        for uid in (1, 2, 3):
            assert client.get(f"/user/{uid}").status_code == 200
        traces = [t for t in _traces(redline) if t.path.startswith("/user/")]
        assert len(traces) == 3
        aliases = set()
        for trace in traces:
            sql = [s for s in trace.spans if s.kind == "sql"]
            assert len(sql) == 2
            assert all(s.data["auto_engine"] for s in sql)
            aliases |= {s.data["db_alias"] for s in sql}
        assert len(aliases) == 3  # one alias per engine, no double instrumentation
    finally:
        redline.close()


def test_default_is_still_explicit(tmp_path):
    app, redline = _app(tmp_path, [])
    try:
        app.test_client().get("/user/1")
        trace = next(t for t in _traces(redline) if t.path.startswith("/user/"))
        assert not [s for s in trace.spans if s.kind == "sql"]
    finally:
        redline.close()


def test_disposed_engines_are_not_leaked(tmp_path):
    made: list = []
    app, redline = _app(tmp_path, made, engines="auto")
    try:
        client = app.test_client()
        for uid in range(1, 6):
            client.get(f"/user/{uid}")
        gc.collect()
        assert all(ref() is None for ref in made)
    finally:
        redline.close()


def test_invalid_engines_value_is_rejected():
    with pytest.raises(ValueError):
        Redline(engines="everything")
