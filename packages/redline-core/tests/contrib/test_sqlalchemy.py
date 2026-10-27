from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from redline_core.contrib.sqlalchemy import (
    build_executor,
    explain_executor_factory,
    instrument_engine,
    name_engines,
    vendor_of,
)


def test_sync_engine_spans(tracer, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    instrument_engine(engine)
    instrument_engine(engine)  # idempotent
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"))
    b = tracer.start()
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO t (name) VALUES (:n)"), {"n": "x"})
        conn.execute(text("SELECT * FROM t WHERE id = :i"), {"i": 1}).fetchall()
    spans = [s for s in tracer.finish(b) if s.kind == "sql"]
    assert len(spans) == 2
    assert spans[0].data["vendor"] == "sqlite"
    assert spans[0].data["db_alias"] == "default"
    assert spans[0].data["rows"] == 1
    assert spans[1].callsite is not None
    assert spans[1].callsite.file.endswith("test_sqlalchemy.py")
    engine.dispose()


def test_no_trace_is_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    instrument_engine(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
    engine.dispose()


async def test_async_engine_spans(tracer, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    instrument_engine(engine, alias="replica")
    b = tracer.start()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1 WHERE 1 = :v"), {"v": 1})
    spans = [s for s in tracer.finish(b) if s.kind == "sql"]
    assert len(spans) == 1
    assert spans[0].data["db_alias"] == "replica"
    assert spans[0].callsite is not None
    assert spans[0].callsite.file.endswith("test_sqlalchemy.py")
    await engine.dispose()


def test_executor_sync_and_naming(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'c.db'}")
    assert explain_executor_factory is build_executor
    run = build_executor(engine, vendor_of(engine), lambda: None)
    assert run("SELECT ?", (7,)) == [(7,)]
    named = name_engines([engine, create_engine(f"sqlite:///{tmp_path / 'analytics.db'}")])
    assert list(named) == ["default", "analytics"]
    engine.dispose()


async def test_executor_async(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
    loop = asyncio.get_running_loop()
    run = build_executor(engine, "sqlite", lambda: loop)
    assert await asyncio.to_thread(run, "SELECT ?", [5]) == [(5,)]
    await engine.dispose()


def test_core_does_not_import_contrib():
    code = "import sys, redline_core; sys.exit('redline_core.contrib' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


@pytest.mark.parametrize("name", ["httpx", "requests", "redis", "asyncpg", "psycopg"])
def test_contrib_modules_import(name):
    __import__(f"redline_core.contrib.{name}")
