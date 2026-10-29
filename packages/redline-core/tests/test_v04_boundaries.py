"""Regression coverage for isolation, privacy and driver cleanup boundaries."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from redline_core import Recorder, Settings, record_query
from redline_core.contrib.sqlalchemy import instrument_engine
from redline_core.model import Span, Trace
from redline_core.session_state import checked_in, observe
from redline_core.storage import SQLiteStore


def test_expected_on_detects_explicit_off():
    recorder = Recorder(Settings(session_expected={"XACT_ABORT": "ON"}))
    with recorder.recording(method="GET", path="/state"):
        info = {}
        span = SimpleNamespace(id="setter", callsite=None, data={"vendor": "mssql"})
        observe(info, "SET XACT_ABORT OFF", span)
        checked_in(info, "default")
    assert "session_state_leak" in recorder.last_trace.issue_kinds


@pytest.mark.parametrize(
    "vendor,sql",
    [
        ("postgresql", "SELECT 'SET search_path TO private'"),
        ("mysql", "SELECT 'SET SESSION sql_mode = private'"),
    ],
)
def test_literal_session_command_is_not_observed(vendor, sql):
    info = {}
    span = SimpleNamespace(id="query", callsite=None, data={"vendor": vendor})
    observe(info, sql, span)
    assert not info.get("redline_session")


def test_literal_reset_does_not_erase_postgres_state():
    info = {}
    span = SimpleNamespace(id="setter", callsite=None, data={"vendor": "postgresql"})
    observe(info, "SET search_path TO private", span)
    before = dict(info["redline_session"])
    observe(info, "SELECT 'RESET ALL'", span)
    assert info["redline_session"] == before


def test_capture_off_preserves_fetched_row_metadata():
    engine = create_engine("sqlite://")
    instrument_engine(engine)
    recorder = Recorder(Settings(capture_sql_params=False, large_result_rows=2))
    try:
        with recorder.recording(method="GET", path="/rows"):
            with engine.connect() as conn:
                assert len(conn.exec_driver_sql("SELECT 1 UNION ALL SELECT 2").fetchall()) == 2
        span = next(s for s in recorder.last_trace.spans if s.kind == "sql")
        assert span.data["params_captured"] is False
        assert span.data["explainable"] is False
        assert span.data["rows"] == 2
        assert "large_result" in recorder.last_trace.issue_kinds
    finally:
        engine.dispose()


def test_auto_discovery_does_not_promote_health_capture_to_explicit_mode():
    automatic = Recorder(Settings(instrument_engines="all"))
    engine = create_engine("sqlite://")
    try:
        with automatic.recording(method="GET", path="/automatic"):
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
        explicit = Recorder(Settings(instrument_engines="explicit"))
        with explicit.recording(method="GET", path="/explicit"):
            with engine.connect() as conn:
                conn.invalidate(Exception("example invalidation"))
        assert explicit.last_trace.query_count == 0
        assert "connection_failure" not in explicit.last_trace.issue_kinds
        instrument_engine(engine, "registered")
        with explicit.recording(method="GET", path="/registered"):
            with engine.connect() as conn:
                conn.invalidate(Exception("example invalidation"))
        assert "connection_failure" in explicit.last_trace.issue_kinds
    finally:
        engine.dispose()


def test_auto_engines_with_same_basename_have_distinct_aliases(tmp_path):
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    engines = [
        create_engine(f"sqlite:///{tmp_path / name / 'app.db'}") for name in ("first", "second")
    ]
    recorder = Recorder(Settings(instrument_engines="all"))
    try:
        with recorder.recording(method="GET", path="/databases"):
            for engine in engines:
                with engine.connect() as conn:
                    conn.exec_driver_sql("SELECT 1")
        aliases = [s.data["db_alias"] for s in recorder.last_trace.spans if s.kind == "sql"]
        assert len(aliases) == len(set(aliases)) == 2
    finally:
        for engine in engines:
            engine.dispose()


def test_mssql_compile_failed_reset_invalidates_and_rolls_back():
    from redline_core.sql_compile import compile_statement

    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def exec_driver_sql(self, sql):
            calls.append(sql)
            if sql == "SET NOEXEC OFF":
                raise RuntimeError("reset failed")

        def execute(self, statement, values):
            calls.append(str(statement))

        def invalidate(self):
            calls.append("invalidate")

        def rollback(self):
            calls.append("rollback")

    engine = SimpleNamespace(dialect=SimpleNamespace(name="mssql"), connect=lambda: Connection())
    with pytest.raises(RuntimeError, match="reset failed"):
        compile_statement(engine, "SELECT :value")
    assert calls[0].startswith("EXEC sys.sp_describe_undeclared_parameters")
    assert "SET NOEXEC ON" in calls
    assert "SELECT @value" in calls
    assert calls[-3:] == ["SET NOEXEC OFF", "invalidate", "rollback"]


def test_unsafe_compile_never_opens_a_connection():
    from redline_core.sql_compile import compile_statement

    def forbidden():
        raise AssertionError("unsafe SQL reached a connection")

    engine = SimpleNamespace(dialect=SimpleNamespace(name="mssql"), connect=forbidden)
    for sql in ("SELECT 1; DELETE FROM t", "SET NOEXEC OFF; UPDATE t SET x=1", "EXEC dbo.proc"):
        assert compile_statement(engine, sql)["status"] == "unresolved"


def test_actual_plan_failure_between_result_sets_restores_settings():
    from redline_core.mssql_actual import collect

    calls = []

    class Cursor:
        description = (("value",),)
        messages = []

        def fetchmany(self, size):
            return []

        def nextset(self):
            raise RuntimeError("driver result-set failure")

    class Result:
        cursor = Cursor()

        def close(self):
            calls.append("close")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def exec_driver_sql(self, sql, params=()):
            calls.append(sql)
            return Result()

        def invalidate(self):
            calls.append("invalidate")

        def rollback(self):
            calls.append("rollback")

    engine = SimpleNamespace(connect=lambda: Connection())
    with pytest.raises(RuntimeError, match="result-set failure"):
        collect(engine, "SELECT 1", None)
    assert calls[-4:] == ["close", "SET STATISTICS XML OFF", "SET STATISTICS IO OFF", "rollback"]


def test_secondary_store_refuses_db_execution_and_remains_read_only(tmp_path):
    from redline_core.api import RedlineAPI

    path = tmp_path / "worker.db"
    worker = SQLiteStore(str(path), background=False)
    sql = Span(
        "sql",
        None,
        "sql",
        "SELECT ?",
        0,
        1,
        data={"sql": "SELECT 1", "vendor": "sqlite", "db_alias": "default", "explainable": True},
    )
    worker.save(Trace("worker-trace", "other", "TASK", "job", spans=[sql]))
    worker.close()
    primary = SQLiteStore.from_settings(
        Settings(storage_path=str(tmp_path / "web.db"), extra_stores=(str(path),)), background=False
    )

    def forbidden(*args):
        raise AssertionError("secondary SQL reached a primary executor")

    try:
        api = RedlineAPI(primary, Settings(), explain_executor=forbidden)
        status, body = api.dispatch(
            "POST", "api/explain", body={"trace_id": "worker-trace", "span_id": "sql"}
        )
        assert status == 403 and body["error"]["code"] == "secondary_store"
        status, body = api.dispatch(
            "POST", "api/analysis/database", body={"trace_id": "worker-trace", "span_id": "sql"}
        )
        assert status == 403 and body["error"]["code"] == "secondary_store"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            primary.readers[0]._query("DELETE FROM traces")
        assert primary.get("worker-trace") is not None
    finally:
        primary.close()


def test_route_redaction_covers_error_frames_logs_and_sql_values(store):
    recorder = Recorder(
        Settings(
            redact_routes={"POST /private": "values"}, project_root=str(__file__.rsplit("/", 1)[0])
        ),
        store,
    )
    builder = recorder.start_trace(
        method="POST", path="/private", route="/private", body='{"notes":"boundary-private-value"}'
    )
    record_query("SELECT ?", ["boundary-private-value"], start=time.perf_counter(), duration=0)
    logging.getLogger("example.private").warning("boundary-private-value")
    try:
        private = "boundary-private-value"
        raise ValueError(private)
    except ValueError as error:
        trace = recorder.finish_trace(
            builder, status=500, error=error, body='{"notes":"boundary-private-value"}'
        )
    assert "boundary-private-value" not in json.dumps(store.get(trace.id).to_dict())
    assert trace.error and trace.error["message"] == "[redacted]"


def test_schema_table_redaction_masks_named_parameters():
    from redline_core.redact import Redactor

    redactor = Redactor(Settings(redact_tables=(r"(?i)medical",)))
    assert redactor.sql_params(
        "UPDATE medical_records SET value=:payload", {"payload": "private-value"}
    ) == {"payload": "[redacted]"}


def test_schema_qualified_table_redaction_masks_positional_parameters():
    from redline_core.redact import Redactor

    redactor = Redactor(Settings(redact_tables=(r"(?i)^medical\.",)))
    assert redactor.sql_params("UPDATE medical.records SET value=?", ["private-value"]) == [
        "[redacted]"
    ]


def test_literal_ignore_directive_cannot_suppress_comment_bind():
    from redline_core.check_sql import lint

    sql = "SELECT 'redline: ignore[bind-in-comment]' -- :value"
    assert "bind-in-comment" in lint(sql, "mssql")


@pytest.mark.parametrize(
    "sql", ["SELECT [OUTPUT] FROM dbo.items", "INSERT INTO dbo.items ([OUTPUT]) VALUES (1)"]
)
def test_output_identifier_is_not_an_output_clause(sql):
    from redline_core.check_sql import lint

    assert "output-without-into" not in lint(sql, "mssql")


def test_changing_default_recorder_does_not_disable_an_active_recorders_logs():
    import subprocess
    import sys

    script = """import json, logging
from redline_core import Recorder, Settings, configure
first = Recorder(Settings(capture_logs=True))
builder = first.start_trace(method="GET", path="/active")
logging.getLogger("example.active").warning("before")
configure(Settings(capture_logs=False))
logging.getLogger("example.active").warning("after")
trace = first.finish_trace(builder, status=200)
print(json.dumps([record.message for record in trace.logs]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == ["before", "after"]


def test_column_profile_masks_a_differently_named_bind():
    from redline_core.redact import Redactor

    redactor = Redactor(Settings(redact_profiles=("health",)))
    assert redactor.sql_params(
        "UPDATE records SET diagnosis=:payload", {"payload": "private-value"}
    ) == {"payload": "[redacted]"}


def test_insert_schema_table_redaction_masks_parameters():
    from redline_core.redact import Redactor

    redactor = Redactor(Settings(redact_tables=(r"(?i)^medical\.",)))
    assert redactor.sql_params(
        "INSERT INTO medical.records (value) VALUES (?)", ["private-value"]
    ) == ["[redacted]"]


def test_sql_junit_counts_match_emitted_cases(tmp_path):
    import io
    from xml.etree import ElementTree as ET

    from redline_core.check_sql import command

    source = tmp_path / "queries.py"
    source.write_text('text("INSERT INTO dbo.t (x) OUTPUT inserted.id VALUES (:x) -- :x")\n')
    destination = tmp_path / "sql-results.xml"
    args = SimpleNamespace(
        paths=[str(source)],
        dialect="mssql",
        dsn=None,
        schema_snapshot=None,
        baseline=None,
        write_baseline=None,
        junit=str(destination),
        json=False,
    )
    assert command(args, io.StringIO(), io.StringIO()) == 1
    suite = ET.parse(destination).getroot()
    assert int(suite.get("tests")) == len(suite.findall("testcase"))
    assert int(suite.get("failures")) == len(suite.findall("testcase/failure"))


def test_sql_directory_enumeration_is_bounded_before_sorting(tmp_path, monkeypatch):
    from pathlib import Path

    from redline_core.check_sql import scan

    consumed = [0]

    def entries(path):
        assert path == tmp_path
        for index in range(20200):
            consumed[0] += 1
            if consumed[0] > 20050:
                raise AssertionError("directory enumeration exceeded the advertised entry cap")
            yield tmp_path / f"ignored-{index}.txt"

    monkeypatch.setattr(Path, "iterdir", entries)
    report = scan([str(tmp_path)])
    assert consumed[0] <= 20001
    assert report["skipped"]


def test_non_numeric_application_tag_cannot_replace_original_sql_error():
    from sqlalchemy.exc import OperationalError

    from redline_core import tag

    engine = create_engine("sqlite://")
    instrument_engine(engine)
    recorder = Recorder()
    try:
        with recorder.recording(method="GET", path="/handled"):
            tag("sql_errors", "unknown")
            with engine.connect() as conn:
                with pytest.raises(OperationalError, match="missing_table"):
                    conn.exec_driver_sql("SELECT * FROM missing_table")
        assert "sql_error" in recorder.last_trace.issue_kinds
    finally:
        engine.dispose()


def test_failed_executemany_preserves_batch_redaction_and_cap():
    import uuid

    from sqlalchemy.exc import IntegrityError

    sentinel = "failed-batch-private-" + uuid.uuid4().hex
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE records (id INTEGER PRIMARY KEY, diagnosis TEXT)")
    instrument_engine(engine)
    recorder = Recorder(Settings(redact_profiles=("health",)))
    rows = [(1, sentinel) for _ in range(50)]
    try:
        with recorder.recording(method="GET", path="/batch"):
            with engine.begin() as conn:
                with pytest.raises(IntegrityError):
                    conn.exec_driver_sql("INSERT INTO records (id, diagnosis) VALUES (?, ?)", rows)
        span = next(s for s in recorder.last_trace.spans if s.kind == "sql")
        assert span.data["many"] is True
        assert len(span.data["params"]) <= 10
        assert sentinel not in json.dumps(recorder.last_trace.to_dict())
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "source",
    [
        'SQL = "SELECT 1 -- :secret"\nSQL = make_query()\nconn.execute(SQL)\n',
        'SQL = "SELECT 1 -- :secret"\ndef run(SQL):\n    conn.execute(SQL)\n',
        'SQL = "SELECT 1 -- :secret"\nSQL, other = "ab"\nconn.execute(SQL)\n',
        'SQL = "SELECT 1 -- :secret"\ndef run():\n    conn.execute(SQL)\n    SQL = make_query()\n',
    ],
)
def test_sql_extraction_does_not_invent_sql_from_rebound_or_shadowed_constants(source):
    from redline_core.check_sql import extract

    statements, skipped = extract(source)
    assert statements == []
    assert skipped and skipped[0]["reason"] == "dynamic SQL"


def test_sql_extraction_respects_module_statement_order():
    from redline_core.check_sql import extract

    source = 'SQL = "SELECT 1 -- :first"\nconn.execute(SQL)\nSQL = "SELECT 2"\nconn.execute(SQL)\n'
    statements, skipped = extract(source)
    assert [statement["sql"] for statement in statements] == ["SELECT 1 -- :first", "SELECT 2"]
    assert skipped == []


def test_sql_extraction_bounds_expanding_constant_f_strings():
    from redline_core.check_sql import extract

    # A short module can otherwise expand constants exponentially without executing it.
    source = 'SQL = "SELECT ' + "x" * 20000 + '"\n'
    source += 'SQL = f"{SQL}{SQL}"\n' * 8
    source += "conn.execute(SQL)\n"
    statements, skipped = extract(source)
    assert statements == []
    assert skipped and skipped[0]["reason"] == "dynamic SQL"


def test_insert_select_star_rule_does_not_cross_statement_boundary():
    from redline_core.check_sql import lint

    assert "select-star-in-insert-select" not in lint("INSERT INTO t VALUES (1); SELECT * FROM t")


@pytest.mark.parametrize(
    "source",
    [
        "conn.execute(make_query())",
        "conn.execute(text(make_query()))",
        (
            'SQL = "SELECT 1"\ndef mutate():\n    global SQL\n'
            "    SQL = make_query()\nmutate()\nconn.execute(SQL)"
        ),
    ],
)
def test_dynamic_sql_calls_are_reported_as_unresolved(source):
    from redline_core.check_sql import analyze_sql_source

    report = analyze_sql_source(source)
    assert report["statements"] == 0
    assert report["skipped"] and report["skipped"][0]["reason"] == "dynamic SQL"


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT OUTPUT FROM t", False),
        ("SELECT 'INSERT OUTPUT'", False),
        ("UPDATE t SET a=1 OUTPUT inserted.a", True),
        ("DELETE FROM t OUTPUT deleted.a INTO audit", False),
        ("SELECT 1; INSERT INTO t VALUES (1) OUTPUT inserted.a", True),
    ],
)
def test_output_lint_requires_a_real_dml_clause(sql, expected):
    from redline_core.check_sql import lint

    assert ("output-without-into" in lint(sql, "mssql")) is expected
