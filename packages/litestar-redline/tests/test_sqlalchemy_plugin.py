"""Engines owned by Litestar's own ``SQLAlchemyPlugin`` are instrumented automatically."""

from __future__ import annotations

import pytest
from litestar import Litestar, get
from litestar.testing import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from litestar_redline import RedlinePlugin
from redline_core.storage import SQLiteStore

try:
    from advanced_alchemy.extensions.litestar import (
        SQLAlchemyAsyncConfig,
        SQLAlchemyPlugin,
        SQLAlchemySyncConfig,
    )
except ImportError:
    try:  # older Litestar re-exports it
        from litestar.plugins.sqlalchemy import (
            SQLAlchemyAsyncConfig,
            SQLAlchemyPlugin,
            SQLAlchemySyncConfig,
        )
    except ImportError:
        pytest.skip("Litestar's SQLAlchemy plugin is not importable", allow_module_level=True)


def _app(tmp_path, config, handler, **kw):
    redline = RedlinePlugin(
        settings={"storage_path": str(tmp_path / "r.db"), "project_root": str(tmp_path)},
        guards=[],
        **kw,
    )
    app = Litestar(route_handlers=[handler], plugins=[SQLAlchemyPlugin(config=config), redline])
    return app, redline


def _only_trace(redline):
    store = SQLiteStore(redline.store.path, background=False)
    return store.get(store.list(None, limit=5).items[0]["id"])


def test_async_plugin_engine_is_autodetected(tmp_path):
    config = SQLAlchemyAsyncConfig(connection_string=f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")

    @get("/count")
    async def count(db_session: AsyncSession) -> dict:
        return {"v": (await db_session.execute(text("SELECT 41 + 1"))).scalar()}

    app, redline = _app(tmp_path, config, count)
    assert redline.engines["default"] is config.get_engine()
    with TestClient(app, base_url="http://localhost") as c:
        assert c.get("/count").json() == {"v": 42}
    sql = [s for s in _only_trace(redline).spans if s.kind == "sql"]
    assert [s.data["sql"] for s in sql] == ["SELECT 41 + 1"]
    assert sql[0].data["vendor"] == "sqlite" and sql[0].data["db_alias"] == "default"


def test_sync_plugin_engine_is_autodetected(tmp_path):
    config = SQLAlchemySyncConfig(connection_string=f"sqlite:///{tmp_path / 's.db'}")

    @get("/count", sync_to_thread=True)
    def count(db_session: Session) -> dict:
        return {"v": db_session.execute(text("SELECT 7")).scalar()}

    app, redline = _app(tmp_path, config, count)
    assert redline.engines["default"] is config.get_engine()
    with TestClient(app, base_url="http://localhost") as c:
        assert c.get("/count").json() == {"v": 7}
    assert _only_trace(redline).query_count == 1


def test_explicit_engine_wins_over_plugin_engine(tmp_path):
    config = SQLAlchemySyncConfig(connection_string=f"sqlite:///{tmp_path / 's.db'}")
    mine = create_engine(f"sqlite:///{tmp_path / 'mine.db'}")

    @get("/x")
    async def x() -> dict:
        return {}

    _, redline = _app(tmp_path, config, x, engine=mine)
    assert redline.engines == {"default": mine}
