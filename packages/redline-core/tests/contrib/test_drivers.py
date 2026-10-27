from __future__ import annotations

import os
from types import SimpleNamespace

import asyncpg
import psycopg
import pytest

from redline_core.contrib.asyncpg import instrument_asyncpg, uninstrument_asyncpg
from redline_core.contrib.psycopg import instrument_psycopg, uninstrument_psycopg

# The hooks are unit-tested against stand-ins patched in *before* instrumenting, so no
# server is needed; the real-server tests below run when REDLINE_TEST_PG_DSN is set.

DSN = os.environ.get("REDLINE_TEST_PG_DSN")


async def test_asyncpg_hooks(tracer, monkeypatch):
    async def execute(self, query, *args, timeout=None):
        return "INSERT 0 3"

    async def fetch(self, query, *args, timeout=None):
        return [1, 2]

    async def fetchrow(self, query, *args, timeout=None):
        return None

    async def fetchval(self, query, *args, timeout=None):
        return 5

    async def executemany(self, command, args, *, timeout=None):
        return None

    for name, fn in (
        ("execute", execute),
        ("fetch", fetch),
        ("fetchrow", fetchrow),
        ("fetchval", fetchval),
        ("executemany", executemany),
    ):
        monkeypatch.setattr(asyncpg.Connection, name, fn)
    assert instrument_asyncpg() and instrument_asyncpg()
    try:
        C, conn = asyncpg.Connection, None
        b = tracer.start()
        await C.execute(conn, "INSERT INTO t VALUES ($1)", 1)
        await C.fetch(conn, "SELECT * FROM t WHERE id = $1", 1)
        await C.fetchrow(conn, "SELECT * FROM t WHERE id = $1", 9)
        assert await C.fetchval(conn, "SELECT count(*) FROM t") == 5
        await C.executemany(conn, "INSERT INTO t VALUES ($1)", [(1,), (2,)])
        spans = tracer.finish(b)
        assert await C.fetch(conn, "SELECT 1") == [1, 2]  # no trace: untouched
    finally:
        uninstrument_asyncpg()
    assert [s.data["rows"] for s in spans] == [3, 2, 0, None, None]
    assert all(s.kind == "sql" and s.data["vendor"] == "postgresql" for s in spans)
    assert spans[1].data["params"] == [1]
    assert spans[4].data["params"] == [[1], [2]]
    assert spans[0].callsite is not None


def test_psycopg_hooks(tracer, monkeypatch):
    def execute(self, query, params=None, **kw):
        self.rowcount = 4
        return self

    def executemany(self, query, params_seq, **kw):
        self.rowcount = -1

    monkeypatch.setattr(psycopg.Cursor, "execute", execute)
    monkeypatch.setattr(psycopg.Cursor, "executemany", executemany)
    assert instrument_psycopg() and instrument_psycopg(None)
    try:
        cur = SimpleNamespace(rowcount=-1)
        b = tracer.start()
        psycopg.Cursor.execute(cur, "SELECT * FROM t WHERE id = %s", (1,))
        psycopg.Cursor.executemany(cur, b"INSERT INTO t VALUES (%s)", [(1,), (2,)])
        spans = tracer.finish(b)
    finally:
        uninstrument_psycopg()
    assert spans[0].data["rows"] == 4
    assert spans[0].data["vendor"] == "postgresql"
    assert spans[0].data["params"] == [1]
    assert spans[1].data["rows"] is None
    assert spans[1].data["sql"] == "INSERT INTO t VALUES (%s)"


async def test_psycopg_async_hooks(tracer, monkeypatch):
    async def execute(self, query, params=None, **kw):
        self.rowcount = 1
        return self

    monkeypatch.setattr(psycopg.AsyncCursor, "execute", execute)
    instrument_psycopg()
    try:
        b = tracer.start()
        await psycopg.AsyncCursor.execute(SimpleNamespace(rowcount=-1), "SELECT 1")
        (span,) = tracer.finish(b)
    finally:
        uninstrument_psycopg()
    assert span.data["rows"] == 1


@pytest.mark.skipif(not DSN, reason="needs REDLINE_TEST_PG_DSN")
def test_psycopg_real(tracer):
    instrument_psycopg()
    try:
        b = tracer.start()
        with psycopg.connect(DSN) as conn:
            conn.execute("SELECT %s", (1,))
        (span,) = tracer.finish(b)
    finally:
        uninstrument_psycopg()
    assert span.data["rows"] == 1


@pytest.mark.skipif(not DSN, reason="needs REDLINE_TEST_PG_DSN")
async def test_asyncpg_real(tracer):
    instrument_asyncpg()
    try:
        conn = await asyncpg.connect(DSN)
        b = tracer.start()
        assert await conn.fetchval("SELECT $1::int", 1) == 1
        (span,) = tracer.finish(b)
        await conn.close()
    finally:
        uninstrument_asyncpg()
    assert span.data["sql"].startswith("SELECT")
