from __future__ import annotations

import gc
import json
import time
import weakref

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from redline_core import Recorder, Settings, current, job, record_query, row_outcome, stage
from redline_core.check_sql import analyze_sql_source, lint
from redline_core.config import resolve_settings
from redline_core.contrib.sqlalchemy import instrument_engine
from redline_core.sql import (
    comment_placeholders,
    effective_kinds,
    fingerprint,
    is_explainable,
    statement_kind,
)
from redline_core.testing import assert_traces
from redline_core.vendors import get_vendor


def test_handled_sql_error_is_kept_and_assertable(store):
    recorder = Recorder(Settings(sample_rate=0), store)
    engine = create_engine("sqlite://")
    instrument_engine(engine)
    builder = recorder.start_trace(method="GET", path="/degraded")
    with engine.connect() as conn:
        try:
            conn.execute(text("SELECT absent FROM no_table"))
        except SQLAlchemyError:
            pass
    trace = recorder.finish_trace(builder, status=200)
    assert trace.status == 200 and trace.error is None
    failed = next(s for s in trace.spans if s.data.get("error"))
    assert failed.data["error"]["type"] == "OperationalError"
    assert not failed.data["explainable"]
    assert store.get(trace.id) is not None
    assert "sql_error" in trace.issue_kinds
    with pytest.raises(AssertionError, match="SQL statement failed"):
        assert_traces([trace], no_sql_errors=True)
    engine.dispose()


def test_comment_bind_survives_execution_failure():
    recorder = Recorder()
    engine = create_engine("sqlite://")
    instrument_engine(engine)
    with recorder.recording(method="GET", path="/"):
        with engine.connect() as conn:
            try:
                conn.execute(text("SELECT :x -- documentation :y"), {"x": 1, "y": 2})
            except SQLAlchemyError:
                pass
    assert "placeholder_in_comment" in recorder.last_trace.issue_kinds
    engine.dispose()


def test_auto_engine_capture_and_collection():
    recorder = Recorder(Settings(instrument_engines="all"))
    with recorder.recording(method="GET", path="/"):
        engine = create_engine("sqlite://")
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    assert recorder.last_trace.query_count == 1
    engine.dispose()
    ref = weakref.ref(engine)
    del conn, engine
    gc.collect()
    assert ref() is None


@pytest.mark.parametrize(
    "sql,count",
    [
        ("SELECT '?' -- :x", 1),
        ("SELECT [a?b]", 0),
        ("SELECT '-- ?'", 0),
        ("/* %(x)s */ SELECT 1", 1),
    ],
)
def test_comment_token_boundaries(sql, count):
    assert comment_placeholders(sql) == count


def test_batch_and_brackets():
    sql = "SET NOCOUNT ON; DECLARE @t TABLE (id int); INSERT INTO t VALUES (1); SELECT * FROM t"
    assert effective_kinds(sql) == ("SET", "DECLARE", "INSERT", "SELECT")
    assert statement_kind(sql) == "INSERT"
    assert not is_explainable(sql)
    assert "[2024]" in fingerprint("SELECT [2024], [a?b], [escaped]]name] FROM t WHERE x=3")
    assert not is_explainable("EXEC dbo.p")


@pytest.mark.parametrize(
    "state,code,category",
    [
        ("42S22", 207, "schema"),
        ("42000", 334, "trigger_output"),
        ("08S01", 10054, "disconnect"),
        ("HYT00", 0, "timeout"),
    ],
)
def test_driver_shaped_errors(state, code, category):
    exc = Exception(state, f"[Driver] diagnostic ({code}) (SQLExecDirectW)")
    error = get_vendor("mssql").classify_error(exc)
    assert error["native"] == code and error["category"] == category


def test_parameter_capture_disabled_removes_inline_literals(store):
    recorder = Recorder(Settings(capture_sql_params=False, capture_locals=False), store)
    with recorder.recording(method="GET", path="/"):
        record_query(
            "INSERT INTO notes VALUES ('SENTINEL-VALUE', ?)",
            ["SENTINEL-VALUE"],
            start=time.perf_counter(),
            duration=0,
        )
    stored = store.get(recorder.last_trace.id)
    assert "SENTINEL-VALUE" not in json.dumps(stored.to_dict())
    assert stored.spans[0].data["params"] is None
    assert not stored.spans[0].data["explainable"]


def test_route_policy_applies_at_finish(store):
    settings = Settings(redact_routes={"POST /private/<id>": "values"})
    recorder = Recorder(settings, store)
    builder = recorder.start_trace(
        method="POST", path="/private/1", body=b'{"children":"SENTINEL"}'
    )
    record_query("UPDATE t SET v=?", ["SENTINEL"], start=time.perf_counter(), duration=0)
    trace = recorder.finish_trace(
        builder, status=200, route="/private/<id>", body='{"children":"SENTINEL"}'
    )
    assert "SENTINEL" not in json.dumps(store.get(trace.id).to_dict())
    assert json.loads(trace.response["body"])["children"] == "[redacted]"


def test_profiles_and_environment():
    from redline_core.redact import Redactor

    redactor = Redactor(Settings(redact_profiles=("pii", "health")))
    assert redactor.mapping({"email": "sentinel", "diagnosis": "sentinel"}) == {
        "email": "[redacted]",
        "diagnosis": "[redacted]",
    }
    resolved = resolve_settings(environ={"REDLINE_CAPTURE_SQL_PARAMS": "reads_only"})
    assert resolved.settings.capture_sql_params == "reads_only"


def test_response_correlation():
    recorder = Recorder()
    builder = recorder.start_trace(method="GET", path="/", headers={"X-Request-ID": "incoming"})
    trace = recorder.finish_trace(builder, status=200, headers={"X-Request-ID": "outgoing"})
    assert trace.tags["request_id"] == "outgoing"


def test_job_context_and_row_payload_privacy():
    recorder = Recorder()
    outer = recorder.start_trace(method="GET", path="/")
    with job("import", recorder=recorder, task_id="task-1"):
        with stage("write"):
            row_outcome("failed", "SENTINEL-REASON", person="SENTINEL-ID")
    assert current() is outer
    trace = recorder.last_trace
    assert trace.method == "TASK" and trace.tags["job.outcome"] == "succeeded"
    assert "SENTINEL" not in json.dumps(trace.to_dict())
    recorder.finish_trace(outer, status=200)


def test_sql_checker_resolves_constants_without_importing():
    source = """from sqlalchemy import text
raise RuntimeError("must not import")
TABLE = "dbo.t"
QUERY = f"INSERT INTO {TABLE} (a) OUTPUT inserted.id VALUES (:a) -- uses :a"
conn.execute(QUERY, {"b": 1})
"""
    report = analyze_sql_source(source, dialect="mssql")
    kinds = {f["kind"] for f in report["findings"]}
    assert kinds == {"bind-in-comment", "output-without-into", "parameter-not-bound"}
    assert report["statements"] == 1
    assert lint("SELECT 1 -- :x redline: ignore[bind-in-comment]") == []


def test_sqlite_online_compile_never_writes():
    from redline_core.sql_compile import compare_schema, compile_statement, schema_snapshot

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id INTEGER)")
    assert compile_statement(engine, "INSERT INTO t VALUES (:id)")["status"] == "compiled"
    assert compile_statement(engine, "SELECT missing FROM t")["status"] == "error"
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM t").scalar() == 0
    snapshot = schema_snapshot(engine)
    assert compare_schema({"tables": {}}, snapshot)["added_tables"] == ["t"]
    engine.dispose()


def test_nested_flask_fastapi_one_trace(store):
    pytest.importorskip("flask_redline")
    pytest.importorskip("fastapi_redline")
    from a2wsgi import WSGIMiddleware
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from flask import Flask

    from fastapi_redline import install
    from flask_redline import Redline as FlaskRedline

    app = FastAPI()
    outer = install(app, store=store, settings={"access": "local", "capture_locals": False})
    flask = Flask(__name__)

    @flask.get("/items/<int:item_id>")
    def item(item_id):
        return {"id": item_id}

    FlaskRedline(flask, recorder=outer.recorder, mode="nested")
    app.mount("/legacy", WSGIMiddleware(flask))
    with TestClient(app) as client:
        assert client.get("/legacy/items/4").status_code == 200
        store.flush()
        traces = store.list(limit=10).items
        assert len(traces) == 1
        trace = store.get(traces[0]["id"])
        assert trace.route == "/legacy/items/<int:item_id>"
        assert trace.tags["nested.framework"] == "flask"


def test_otlp_preview_has_no_payload_or_identifier_values():
    from redline_core.model import Trace
    from redline_core.otlp import export

    trace = Trace("id", "flask", "GET", "/SENTINEL")
    trace.request["body"] = "SENTINEL"
    trace.tags["request_id"] = "SENTINEL"
    assert "SENTINEL" not in json.dumps(export([trace], None))


def test_sql_api_is_bounded_and_obeys_access(store):
    from redline_core.api import RedlineAPI

    api = RedlineAPI(store, Settings(analysis_from_ui=True))
    status, report = api.dispatch(
        "POST", "api/analysis/sql", body={"source": 'text("SELECT 1 -- :x")', "dialect": "mssql"}
    )
    assert status == 200 and report["findings"][0]["kind"] == "bind-in-comment"
    status, _ = RedlineAPI(store, Settings(analysis_from_ui=True, read_only=True)).dispatch(
        "POST", "api/analysis/sql", body={"source": ""}
    )
    assert status == 403


def test_mssql_plan_parser_and_executor_cleanup():
    from redline_core.contrib.sqlalchemy import build_executor
    from redline_core.explain import ExplainError, run_explain
    from redline_core.explain_mssql import analyze_mssql

    xml = (
        '<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07'
        '/showplan"><BatchSequence><Batch><Statements><StmtSimple '
        'StatementSubTreeCost="4"><QueryPlan><Warnings><PlanAffectingConver'
        't Expression="CONVERT_IMPLICIT(int,[t].[x],0)"/></Warnings><RelOp '
        'PhysicalOp="Table Scan" EstimateRows="5000" '
        'EstimatedTotalSubtreeCost="4"/><MissingIndexes><MissingIndexGroup '
        'Impact="90"><MissingIndex Schema="[dbo]" '
        'Table="[items]"><ColumnGroup Usage="EQUALITY"><Column '
        'Name="[id]"/></ColumnGroup><ColumnGroup Usage="INCLUDE"><Column '
        'Name="[name]"/></ColumnGroup></MissingIndex></MissingIndexGroup></'
        "MissingIndexes></QueryPlan></StmtSimple></Statements></Batch></Bat"
        "chSequence></ShowPlanXML>"
    )
    analysis = analyze_mssql(xml)
    assert "implicit_conversion" in analysis.tree.flags
    assert "INCLUDE ([name])" in analysis.suggested_indexes[0]
    with pytest.raises(ExplainError, match="cannot safely toggle"):
        run_explain("SELECT 1", None, "mssql", lambda *_: [])
    calls = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def exec_driver_sql(self, sql, params=()):
            calls.append(sql)
            if sql == "SELECT 1":
                raise RuntimeError("compile failed")
            return self

        def rollback(self):
            calls.append("rollback")

        def invalidate(self):
            calls.append("invalidate")

    class Engine:
        def connect(self):
            return Conn()

    executor = build_executor(Engine(), "mssql", lambda: None)
    with pytest.raises(RuntimeError):
        executor.mssql_estimated_plan("SELECT 1", None)
    assert calls == ["SET SHOWPLAN_XML ON", "SELECT 1", "SET SHOWPLAN_XML OFF", "rollback"]


def test_secondary_store_read_only_browsing(tmp_path):
    from redline_core.model import Trace
    from redline_core.storage import SQLiteStore

    worker_path = tmp_path / "worker.db"
    worker = SQLiteStore(str(worker_path), background=False)
    trace = Trace("worker-trace", "other", "TASK", "import", started_at=1)
    worker.save(trace)
    worker.close()
    store = SQLiteStore.from_settings(
        Settings(storage_path=str(tmp_path / "web.db"), extra_stores=(str(worker_path),)),
        background=False,
    )
    try:
        assert store.list(limit=10).items[0]["id"] == trace.id
        assert store.get(trace.id).tags["secondary_store"] == "1"
        assert store.delete(trace.id) == 0
        assert store.get(trace.id) is not None
    finally:
        store.close()


def test_caught_error_retained_after_span_cap(store):
    recorder = Recorder(Settings(max_spans_per_trace=1, sample_rate=0), store)
    engine = create_engine("sqlite://")
    instrument_engine(engine)
    with recorder.recording(method="GET", path="/"):
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
            try:
                conn.exec_driver_sql("SELECT absent FROM missing")
            except SQLAlchemyError:
                pass
    trace = store.get(recorder.last_trace.id)
    assert trace is not None and "sql_error" in trace.issue_kinds
    assert trace.truncated and trace.tags["sql_errors"] == "1"
    engine.dispose()


def test_user_resolver_and_request_id_before_job():
    from types import SimpleNamespace

    from redline_core.jobs import propagation_headers
    from redline_core.users import from_attr

    recorder = Recorder(Settings(user_resolver=from_attr("state.user_id")))
    builder = recorder.start_trace(
        method="GET", path="/", headers=[(b"x-request-id", b"request-1")]
    )
    assert propagation_headers()["request_id"] == "request-1"
    trace = recorder.finish_trace(
        builder, status=200, request_object=SimpleNamespace(state=SimpleNamespace(user_id=42))
    )
    assert trace.tags["user_id"] == "42"


def test_no_network_while_recording(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected outbound connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    recorder = Recorder(Settings(capture_sql_params=False))
    with recorder.recording(method="GET", path="/"):
        record_query("SELECT ?", [1], start=time.perf_counter(), duration=0)
    assert recorder.last_trace.query_count == 1


def test_session_expectations_and_reset():
    from types import SimpleNamespace

    from redline_core.session_state import checked_in, observe

    recorder = Recorder()
    with recorder.recording(method="GET", path="/"):
        info = {}
        span = SimpleNamespace(id="set-1", callsite=None, data={"vendor": "mssql"})
        observe(info, "SET ANSI_NULLS ON; SET NOCOUNT ON", span)
        checked_in(info, "default")
        observe(info, "SET NOCOUNT OFF", span)
        checked_in(info, "default")
    findings = [i for i in recorder.last_trace.issues if i.kind == "session_state_leak"]
    assert len(findings) == 1
    assert findings[0].count == 1
    evidence = next(
        s.data["session_state_leak"]
        for s in recorder.last_trace.spans
        if s.data.get("session_state_leak")
    )
    assert set(evidence) == {"NOCOUNT"}


def test_connection_observations_expire_and_do_not_invent_causality(monkeypatch):
    from redline_core import connection_health
    from redline_core.detect import Detector
    from redline_core.model import Span, Trace

    class Pool:
        pass

    pool = Pool()
    now = [1000.0]
    monkeypatch.setattr(connection_health, "_clock", lambda: now[0])
    for _ in range(101):
        connection_health.connected(pool)
    for _ in range(3):
        observed = connection_health.evidence(pool, 1, 1, {"is_disconnect": True})
    span = Span(
        "s",
        None,
        "sql",
        "SELECT ?",
        0,
        1,
        data={"sql": "SELECT 1", "connection": {**observed, "pool_recycle": 280}},
    )
    trace = Trace("t", "other", "GET", "/", spans=[span])
    kinds = {i.kind for i in Detector().detect(trace)}
    assert {"fresh_connection_failure", "connection_churn"} <= kinds
    now[0] += 3601
    assert connection_health.evidence(pool, 10, 2, None)["observed_connects_hour"] == 0


def test_actual_mssql_plan_cleans_up_after_fetch_failure():
    from redline_core.mssql_actual import collect

    calls = []

    class Result:
        cursor = None

        def close(self):
            calls.append("result closed")

    result = Result()

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def exec_driver_sql(self, sql, params=()):
            calls.append(sql)
            return result

        def invalidate(self):
            calls.append("invalidated")

        def rollback(self):
            calls.append("rollback")

    class Engine:
        def connect(self):
            return Conn()

    from redline_core.explain import ExplainError

    with pytest.raises(ExplainError, match="multiple result"):
        collect(Engine(), "SELECT 1", None)
    assert calls[-3:] == ["SET STATISTICS XML OFF", "SET STATISTICS IO OFF", "rollback"]
    assert "result closed" in calls


def test_celery_signal_boundary_without_a_broker(monkeypatch):
    import sys
    from types import SimpleNamespace

    from redline_core.contrib.celery import instrument_celery

    class Signal:
        def __init__(self):
            self.receivers = []

        def connect(self, receiver, **kwargs):
            self.receivers.append(receiver)

        def disconnect(self, receiver):
            self.receivers.remove(receiver)

    signals = SimpleNamespace(
        **{
            name: Signal()
            for name in (
                "task_prerun",
                "task_postrun",
                "task_failure",
                "task_retry",
                "before_task_publish",
            )
        }
    )
    monkeypatch.setitem(sys.modules, "celery", SimpleNamespace(signals=signals))
    app = SimpleNamespace(tasks={"sample": True})
    recorder = Recorder(Settings(capture_logs=False, capture_locals=False))
    integration = instrument_celery(app, recorder)
    task = SimpleNamespace(
        app=app,
        name="sample",
        request=SimpleNamespace(delivery_info={"routing_key": "imports"}, headers={}, retries=0),
    )
    integration.pre(task_id="job-1", task=task)
    integration.failure(task_id="job-1", exception=ValueError("example"))
    integration.post(task_id="job-1", state="FAILURE")
    assert current() is None
    assert recorder.last_trace.tags["job.outcome"] == "failed"
    assert "application_error" in recorder.last_trace.issue_kinds
    integration.uninstall()
    assert all(not signal.receivers for signal in vars(signals).values())


def test_capture_policy_also_scrubs_generated_fix_source(store):
    from redline_core import Suggestion

    class Suggester:
        def suggest(self, issue, spans):
            return Suggestion(
                "Review the repeated query", code="PRIVATE-SOURCE", diff="PRIVATE-SOURCE"
            )

    recorder = Recorder(Settings(capture_sql_params=False), store, [Suggester()])
    with recorder.recording(method="GET", path="/"):
        for value in range(5):
            record_query(
                "SELECT id FROM t WHERE id=?", [value], start=time.perf_counter(), duration=0
            )
    trace = store.get(recorder.last_trace.id)
    assert "n_plus_one" in trace.issue_kinds
    assert "duplicate_query" not in trace.issue_kinds
    assert "PRIVATE-SOURCE" not in json.dumps(trace.to_dict())


def test_mssql_catalog_matches_ui_index_contract():
    from redline_core.database_mssql import inspect_query

    seen = []

    def executor(sql, params):
        seen.append(sql)
        if "sys.columns c ON c.object_id=o.object_id" in sql:
            return [("id", "int", 4, None), ("notes", "nvarchar", -1, None)]
        if "sys.indexes i" in sql:
            return [("ix_items", "id", 1, False), ("ix_items", "notes", 0, True)]
        return [("audit_trigger",)]

    report = inspect_query("INSERT INTO dbo.items (id) OUTPUT inserted.id VALUES (1)", executor)
    assert report["table"] == "dbo.items"
    assert report["indexes"] == [
        {"name": "ix_items", "columns": ["id"], "included_columns": ["notes"], "kind": "btree"}
    ]
    assert any(f["kind"] == "output_without_into" for f in report["findings"])
    assert all("SELECT TOP" in query for query in seen)


def test_correlation_respects_explicit_redaction():
    recorder = Recorder(
        Settings(
            redact_headers=("X-Request-ID",),
            redact_keys=("user_id",),
            user_resolver=lambda request: "private-id",
        )
    )
    builder = recorder.start_trace(method="GET", path="/", headers={"X-Request-ID": "private-id"})
    trace = recorder.finish_trace(builder, status=200, request_object=object())
    assert "private-id" not in json.dumps(trace.to_dict())
    assert "request_id" not in trace.tags and trace.tags["user_id"] == "[redacted]"
