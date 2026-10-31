from __future__ import annotations

import gc
import weakref

import pytest
from litestar import Litestar, get
from litestar.testing import TestClient
from sqlalchemy import create_engine, text

from litestar_redline import RedlinePlugin

from .conftest import ROOT


def _app(tmp_path, made, **kw):
    @get("/user/{uid:int}", sync_to_thread=False)
    def user(uid: int) -> dict:
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
    plugin = RedlinePlugin(settings=cfg, **kw)
    return Litestar([user], plugins=[plugin]), plugin


def _user_traces(plugin):
    plugin.store.flush(5)
    items = plugin.store.list(None, limit=50).items
    return [plugin.store.get(t["id"]) for t in items if t["path"].startswith("/user/")]


def test_per_request_engines_are_captured_with_distinct_aliases(tmp_path):
    app, plugin = _app(tmp_path, [], engines="auto")
    with TestClient(app) as client:
        for uid in (1, 2, 3):
            assert client.get(f"/user/{uid}").status_code == 200
        traces = _user_traces(plugin)
    assert len(traces) == 3
    aliases = set()
    for trace in traces:
        sql = [s for s in trace.spans if s.kind == "sql"]
        assert len(sql) == 2 and all(s.data["auto_engine"] for s in sql)
        aliases |= {s.data["db_alias"] for s in sql}
    assert len(aliases) == 3


def test_default_is_still_explicit(tmp_path):
    app, plugin = _app(tmp_path, [])
    with TestClient(app) as client:
        client.get("/user/1")
        trace = _user_traces(plugin)[0]
    assert not [s for s in trace.spans if s.kind == "sql"]


def test_disposed_engines_are_not_leaked(tmp_path):
    made: list = []
    app, _plugin = _app(tmp_path, made, engines="auto")
    with TestClient(app) as client:
        for uid in range(1, 6):
            client.get(f"/user/{uid}")
    gc.collect()
    assert all(ref() is None for ref in made)


def test_invalid_engines_value_is_rejected():
    with pytest.raises(ValueError):
        RedlinePlugin(engines="everything")
