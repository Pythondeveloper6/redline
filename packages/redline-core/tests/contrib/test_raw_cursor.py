from __future__ import annotations

import sqlite3
import types

from sqlalchemy import create_engine, text

from redline_core.contrib.dbapi import instrument_dbapi, uninstrument_dbapi
from redline_core.contrib.sqlalchemy import instrument_engine


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'raw.db'}")
    instrument_engine(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"))
    return engine


def test_engine_and_raw_cursor_statements_are_both_recorded(tracer, tmp_path):
    engine = _engine(tmp_path)
    b = tracer.start()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO t (name) VALUES (:n)"), {"n": "a"})
    raw = engine.raw_connection()
    cur = raw.cursor()
    cur.execute("SELECT * FROM t WHERE id = ?", (1,))
    assert cur.fetchall() == [(1, "a")]
    raw.close()
    spans = [s for s in tracer.finish(b) if s.kind == "sql"]
    assert len(spans) == 2
    assert spans[0].data.get("source") != "raw_cursor"
    assert spans[1].data["source"] == "raw_cursor"
    assert spans[1].data["vendor"] == "sqlite"
    assert spans[1].callsite is not None
    assert spans[1].callsite.file.endswith("test_raw_cursor.py")
    engine.dispose()


def test_sqlalchemy_own_connections_are_not_double_recorded(tracer, tmp_path):
    engine = _engine(tmp_path)
    b = tracer.start()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        conn.execute(text("SELECT 2"))
    spans = [s for s in tracer.finish(b) if s.kind == "sql"]
    assert len(spans) == 2
    assert all(s.data.get("source") != "raw_cursor" for s in spans)
    engine.dispose()


def test_executemany_is_one_span_with_a_batch_size(tracer, tmp_path):
    engine = _engine(tmp_path)
    b = tracer.start()
    raw = engine.raw_connection()
    cur = raw.cursor()
    cur.executemany("INSERT INTO t (name) VALUES (?)", [(f"n{i}",) for i in range(50)])
    raw.commit()
    raw.close()
    spans = [s for s in tracer.finish(b) if s.kind == "sql"]
    assert len(spans) == 1
    assert spans[0].data["many"] is True
    assert spans[0].data["batch_size"] == 50
    engine.dispose()


def test_raw_cursor_error_is_reraised_and_counted(tracer, tmp_path):
    engine = _engine(tmp_path)
    b = tracer.start()
    raw = engine.raw_connection()
    try:
        raw.cursor().execute("SELECT * FROM missing_table")
    except sqlite3.OperationalError:
        pass
    else:  # pragma: no cover
        raise AssertionError("driver error must propagate unchanged")
    raw.close()
    spans = [s for s in tracer.finish(b) if s.kind == "sql"]
    assert len(spans) == 1
    assert "error" in spans[0].data
    engine.dispose()


def test_no_trace_is_noop_and_proxy_forwards_attributes(tmp_path):
    engine = _engine(tmp_path)
    raw = engine.raw_connection()
    cur = raw.cursor()
    cur.execute("SELECT 1")
    assert cur.fetchone() == (1,)
    assert cur.description is not None
    assert list(raw.cursor().execute("SELECT 2")) == [(2,)]  # iteration is forwarded
    raw.close()
    engine.dispose()


def test_instrument_dbapi_records_direct_driver_use(tracer, tmp_path):
    module = types.SimpleNamespace(connect=sqlite3.connect, __name__="sqlite3")
    assert instrument_dbapi(module) is True
    assert instrument_dbapi(module) is True  # idempotent
    b = tracer.start()
    conn = module.connect(str(tmp_path / "d.db"))
    conn.execute("CREATE TABLE x (a INTEGER)")
    cur = conn.cursor()
    cur.executemany("INSERT INTO x VALUES (?)", [(1,), (2,), (3,)])
    cur.execute("SELECT a FROM x")
    assert len(cur.fetchall()) == 3
    conn.close()
    spans = [s for s in tracer.finish(b) if s.kind == "sql"]
    assert len(spans) == 3
    assert {s.data["source"] for s in spans} == {"raw_cursor"}
    assert spans[0].data["vendor"] == "sqlite"
    uninstrument_dbapi(module)
    assert module.connect is sqlite3.connect
