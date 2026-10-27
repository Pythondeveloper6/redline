from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from flask_redline import Redline, uninstrument_httpx

from .shop_app import Base, build_app, holder, seed

ROOT = Path(__file__).resolve().parent.parent


class BufferedClient(FlaskClient):
    """Reads each response to the end, so the trace has finished when the call returns.

    A real WSGI server iterates and closes the response; Werkzeug's test client only does
    that lazily. Pass ``buffered=False`` to exercise the streaming path.
    """

    def open(self, *args, buffered: bool = True, **kwargs):
        return super().open(*args, buffered=buffered, **kwargs)


class Env:
    def __init__(self, app: Flask, redline: Redline, engine):
        self.app = app
        self.redline = redline
        self.engine = engine

    def client(self) -> BufferedClient:
        return self.app.test_client()

    @property
    def store(self):
        live = self.redline.store
        live.flush(5)
        return live

    def traces(self) -> list[dict]:
        return self.store.list(None, limit=500).items

    def trace(self, path: str | None = None, index: int = 0):
        """Full trace; newest first, optionally filtered by concrete path."""
        items = [t for t in self.traces() if path is None or t["path"] == path]
        return self.store.get(items[index]["id"])


@pytest.fixture
def make_env(tmp_path):
    created = []

    def _make(*, settings: dict | None = None, factory: bool = False, **kw):
        cfg = {
            "storage_path": str(tmp_path / f"redline{len(created)}.db"),
            "project_root": str(ROOT),
            **(settings or {}),
        }
        engine = create_engine(f"sqlite:///{tmp_path / 'shop.db'}")
        Base.metadata.create_all(engine)
        holder.sync = sessionmaker(engine, expire_on_commit=False)
        with holder.sync() as s:
            seed(s)
        app = build_app()
        app.test_client_class = BufferedClient
        kw.setdefault("engine", engine)
        if factory:
            redline = Redline(settings=cfg, **kw)
            redline.init_app(app)
        else:
            redline = Redline(app, settings=cfg, **kw)
        env = Env(app, redline, engine)
        created.append(env)
        return env

    yield _make
    for env in created:
        env.redline.close()
        env.engine.dispose()
    uninstrument_httpx()
