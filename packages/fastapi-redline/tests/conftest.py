from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from fastapi_redline import install, uninstrument_httpx
from redline_core.storage import SQLiteStore

from . import shop_app
from .shop_app import Base, build_app, holder, seed

ROOT = Path(__file__).resolve().parent.parent


class Env:
    def __init__(self, app, redline, sync_engine, async_engine):
        self.app = app
        self.redline = redline
        self.sync_engine = sync_engine
        self.async_engine = async_engine

    @property
    def store(self):
        """The live store, or a reader on the same file after the app shut down."""
        live = self.redline.store
        if getattr(live, "_closed", False):
            return SQLiteStore(live.path, background=False)
        live.flush(5)
        return live

    def traces(self) -> list[dict]:
        return self.store.list(None, limit=500).items

    def trace(self, path: str | None = None, index: int = 0):
        """Full trace; newest first, optionally filtered by concrete path."""
        items = [t for t in self.traces() if path is None or t["path"] == path]
        return self.store.get(items[index]["id"])


def allow_all() -> None:
    """A dependency that lets every request in: the tests' default ``framework`` hook (an
    empty ``dependencies=[]`` is no hook since 0.2, and no longer opens Redline)."""


def _access_given(cfg: dict) -> bool:
    """Whether a test chose the access policy itself (settings, preset or environment)."""
    import os

    keys = {k.lower() for k in cfg}
    env = ("REDLINE_ACCESS", "REDLINE_PRESET", "REDLINE_CONFIG")
    return bool({"access", "preset"} & keys) or any(e in os.environ for e in env)


@pytest.fixture
def make_env(tmp_path):
    created = []

    def _make(*, settings: dict | None = None, dependencies=(allow_all,), engine_mode="both", **kw):
        cfg = {
            "storage_path": str(tmp_path / f"redline{len(created)}.db"),
            "project_root": str(ROOT),
            **(settings or {}),
        }
        # Set explicitly, as an application must: an unset `access` answers 404 outside
        # debug mode (CONTRACT.md §18).
        if not dependencies and not _access_given(cfg):
            cfg["access"] = "local"
        sync_engine = create_engine(f"sqlite:///{tmp_path / 'sync.db'}")
        async_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
        Base.metadata.create_all(sync_engine)
        holder.sync = sessionmaker(sync_engine, expire_on_commit=False)
        holder.asyn = async_sessionmaker(async_engine, expire_on_commit=False)
        with holder.sync() as s:
            seed(s)
        app: FastAPI = build_app()
        engines = {
            "both": [async_engine, sync_engine],
            "async": async_engine,
            "sync": sync_engine,
        }[engine_mode]
        redline = install(
            app,
            engine=engines,
            settings=cfg,
            models=shop_app.Base,
            dependencies=dependencies if dependencies is None else list(dependencies),
            **kw,
        )
        env = Env(app, redline, sync_engine, async_engine)
        created.append(env)
        return env

    yield _make
    for env in created:
        env.redline.close()
        env.sync_engine.dispose()
    uninstrument_httpx()
