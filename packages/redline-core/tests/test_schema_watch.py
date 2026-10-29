from __future__ import annotations

import argparse
import io
import json

from sqlalchemy import create_engine

from redline_core import schema_watch
from redline_core.storage.sqlite import SQLiteStore

SOURCE = """
from sqlalchemy import text


def read_a(conn):
    return conn.execute(text("SELECT id FROM a")).fetchall()


def read_b(conn):
    return conn.execute(text("SELECT id FROM b")).fetchall()
"""


def _project(tmp_path):
    database = tmp_path / "schema.db"
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE a (id INTEGER)")
            conn.exec_driver_sql("CREATE TABLE b (id INTEGER)")
    finally:
        engine.dispose()
    source = tmp_path / "queries.py"
    source.write_text(SOURCE)
    return database, source


def test_schema_watch_baseline_then_clean_then_drift(tmp_path):
    database, source = _project(tmp_path)
    dsn = f"sqlite:///{database}"
    snapshot = tmp_path / "schema.json"
    baseline = schema_watch.run_once([str(source)], dsn, str(snapshot))
    assert baseline["baseline"] and not baseline["alert"]
    assert baseline["compiled"] == 2
    assert snapshot.exists()

    unchanged = schema_watch.run_once([str(source)], dsn, str(snapshot))
    assert unchanged["clean"] and not unchanged["alert"]
    assert unchanged["compiled"] == 0

    engine = create_engine(dsn)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE a ADD COLUMN name TEXT")
    finally:
        engine.dispose()
    changed = schema_watch.run_once([str(source)], dsn, str(snapshot))
    assert changed["alert"] and not changed["clean"]
    assert changed["drift"]["affected_tables"] == ["a"]
    assert changed["compiled"] == 1
    assert changed["skipped_unchanged"] == 1


def test_schema_watch_persists_a_ui_issue(tmp_path):
    database, source = _project(tmp_path)
    dsn = f"sqlite:///{database}"
    snapshot = tmp_path / "schema.json"
    store_path = tmp_path / "redline.db"
    assert schema_watch.run_once([str(source)], dsn, str(snapshot))["baseline"]
    engine = create_engine(dsn)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE b ADD COLUMN name TEXT")
    finally:
        engine.dispose()
    report = schema_watch.run_once([str(source)], dsn, str(snapshot))
    assert report["alert"]
    schema_watch.persist(str(store_path), report)

    store = SQLiteStore(str(store_path))
    try:
        groups = store.issues(3600, kind="schema_drift")
        assert groups and groups[0].severity == "warning"
        assert groups[0].sample_trace_id
        trace = store.get(groups[0].sample_trace_id)
        assert trace is not None and trace.path == "redline.schema-watch"
        assert trace.issues[0].kind == "schema_drift"
        assert trace.issues[0].extra["affected_tables"] == ["b"]
    finally:
        store.close()


def test_schema_watch_command_json_and_exit_code(tmp_path):
    database, source = _project(tmp_path)
    dsn = f"sqlite:///{database}"
    snapshot = tmp_path / "schema.json"
    args = argparse.Namespace(
        paths=[str(source)],
        dsn=dsn,
        snapshot=str(snapshot),
        interval=0,
        full=False,
        store=None,
        json=True,
    )
    out, err = io.StringIO(), io.StringIO()
    assert schema_watch.command(args, out, err) == 0
    assert json.loads(out.getvalue())["baseline"] is True

    engine = create_engine(dsn)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE a ADD COLUMN name TEXT")
    finally:
        engine.dispose()
    out, err = io.StringIO(), io.StringIO()
    assert schema_watch.command(args, out, err) == 1
    assert json.loads(out.getvalue())["alert"] is True
    assert not err.getvalue()
