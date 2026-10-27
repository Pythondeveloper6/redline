"""Transaction spans (core helper + SQLAlchemy events), pool waits and their detectors."""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from redline_core.blocking import track_request
from redline_core.conf import Settings
from redline_core.contrib.sqlalchemy import instrument_engine
from redline_core.detectors import REGISTRY
from redline_core.detectors import transactions as tx_detector
from redline_core.model import Span, Trace, new_id
from redline_core.recorder import Recorder, record_span
from redline_core.transactions import begin_transaction, end_transaction

ROOT = os.path.dirname(os.path.dirname(__file__))


def make_recorder(**settings) -> Recorder:
    return Recorder(Settings.from_mapping({"project_root": ROOT, **settings}))


def kinds(trace: Trace, kind: str) -> list[Span]:
    return [s for s in trace.spans if s.kind == kind]


def http_call(url: str = "https://api.stripe.com/v1/charges", ms: float = 5.0) -> None:
    record_span(
        "http_out",
        f"POST {url.split('://', 1)[1]}",
        time.perf_counter(),
        ms / 1000,
        {"method": "POST", "url": url, "status": 200},
    )


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'tx.db'}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"))
    instrument_engine(eng)
    yield eng
    eng.dispose()


# ── core helper ──────────────────────────────────────────────────────────────────────


def test_helper_counts_queries_and_http_and_marks_sql():
    recorder = make_recorder()
    with recorder.recording(method="POST", path="/pay"):
        before = record_span("sql", "SELECT 0", time.perf_counter(), 0.001, {"sql": "SELECT 0"})
        state = begin_transaction()
        record_span("sql", "UPDATE x", time.perf_counter(), 0.001, {"sql": "UPDATE x"})
        other = record_span(
            "sql", "SELECT 1", time.perf_counter(), 0.001, {"sql": "SELECT 1", "db_alias": "b"}
        )
        http_call()
        span = end_transaction(state, db_alias="default", outcome="commit")
    assert span is not None
    assert span.data == {"db_alias": "default", "outcome": "commit", "queries": 1, "http_out": 1}
    assert "in_transaction" not in before.data and "in_transaction" not in other.data
    [inside] = [s for s in recorder.last_trace.spans if s.data.get("in_transaction")]
    assert inside.data["sql"] == "UPDATE x"


def test_helper_skips_empty_transactions_and_no_trace():
    assert begin_transaction() is None
    assert end_transaction(None, db_alias="default", outcome="commit") is None
    recorder = make_recorder()
    with recorder.recording(method="GET", path="/x"):
        state = begin_transaction()
        assert end_transaction(state, db_alias="default", outcome="rollback") is None
        state = begin_transaction()
        assert end_transaction(state, db_alias="default", outcome="rollback", keep_empty=True)


# ── SQLAlchemy ───────────────────────────────────────────────────────────────────────


def test_sqlalchemy_commit_transaction_span(engine):
    recorder = make_recorder()
    with recorder.recording(method="POST", path="/x"):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO t (name) VALUES ('a')"))
            conn.execute(text("SELECT * FROM t")).all()
    trace = recorder.last_trace
    [tx] = kinds(trace, "transaction")
    assert tx.data == {"db_alias": "default", "outcome": "commit", "queries": 2, "http_out": 0}
    assert all(s.data["in_transaction"] is True for s in kinds(trace, "sql"))
    assert tx.callsite is not None and tx.callsite.file == "tests/test_transactions.py"
    sql = kinds(trace, "sql")
    assert tx.start_ms <= sql[0].start_ms
    assert tx.start_ms + tx.duration_ms >= sql[-1].start_ms + sql[-1].duration_ms


def test_sqlalchemy_rollback_and_session(engine):
    recorder = make_recorder()
    with recorder.recording(method="POST", path="/x"):
        with pytest.raises(RuntimeError), engine.begin() as conn:
            conn.execute(text("INSERT INTO t (name) VALUES ('a')"))
            raise RuntimeError
        with Session(engine) as session:
            session.execute(text("INSERT INTO t (name) VALUES ('b')"))
            with session.begin_nested():  # a savepoint: no span of its own
                session.execute(text("INSERT INTO t (name) VALUES ('c')"))
            session.commit()
    outcomes = [
        (s.data["outcome"], s.data["queries"]) for s in kinds(recorder.last_trace, "transaction")
    ]
    assert outcomes[0] == ("rollback", 1)
    assert outcomes[1][0] == "commit" and outcomes[1][1] >= 2
    assert len(outcomes) == 2


def test_sqlalchemy_no_trace_no_state(engine):
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
        assert "_redline_tx" not in conn.__dict__


async def test_sqlalchemy_async_engine_transaction(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    instrument_engine(engine, alias="main")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
    recorder = make_recorder()
    builder = recorder.start_trace(method="POST", path="/x")
    async with track_request(builder):
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO t DEFAULT VALUES"))
            await conn.execute(text("SELECT * FROM t"))
    trace = recorder.finish_trace(builder, status=200)
    [tx] = kinds(trace, "transaction")
    assert tx.data["outcome"] == "commit" and tx.data["queries"] == 2
    assert tx.data["db_alias"] == "main"
    # an async driver awaits: not a sync call on the loop
    assert not [s for s in kinds(trace, "blocking") if s.data["reason"] == "sync_call"]
    await engine.dispose()


async def test_sync_engine_on_the_event_loop_is_a_sync_call(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}")

    @event.listens_for(engine, "connect")
    def add_sleep(dbapi_conn, record):
        dbapi_conn.create_function("pause", 1, lambda s: time.sleep(s) or 1)

    instrument_engine(engine)
    recorder = make_recorder()
    builder = recorder.start_trace(method="GET", path="/x", framework="fastapi")
    async with track_request(builder):
        await asyncio.sleep(0.01)
        with engine.connect() as conn:
            conn.execute(text("SELECT pause(0.05)")).all()
        await asyncio.sleep(0.01)
    trace = recorder.finish_trace(builder, status=200)
    blocking = kinds(trace, "blocking")
    assert [s.data["reason"] for s in blocking] == ["sync_call"]  # loop lag deduplicated
    assert blocking[0].data["call"] == "SQLAlchemy (sync)"
    [issue] = [i for i in trace.issues if i.kind == "blocking_in_async"]
    assert "SQLAlchemy (sync)" in issue.title
    engine.dispose()


def test_sqlalchemy_http_inside_transaction_is_an_issue(engine):
    recorder = make_recorder()
    with recorder.recording(method="POST", path="/pay"):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO t (name) VALUES ('a')"))
            http_call()
        http_call("https://example.com/after")  # after commit: fine
    trace = recorder.last_trace
    [tx] = kinds(trace, "transaction")
    assert tx.data["http_out"] == 1
    [issue] = [i for i in trace.issues if i.kind == "external_in_transaction"]
    assert issue.title.startswith("HTTP call to api.stripe.com inside a transaction (")
    assert issue.title.endswith("tests/test_transactions.py:" + str(issue.callsite.line) + ")")
    assert "on_commit" in issue.suggestion.summary


def test_sqlalchemy_long_transaction(engine):
    recorder = make_recorder(long_transaction_ms=20)
    with recorder.recording(method="POST", path="/x"):
        with engine.begin() as conn:
            conn.execute(text("SELECT 1"))
            time.sleep(0.03)
    [issue] = [i for i in recorder.last_trace.issues if i.kind == "long_transaction"]
    assert issue.severity == "warning"
    assert issue.title.startswith("Transaction open ") and " on default at " in issue.title


def test_pool_wait_under_contention(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'p.db'}", poolclass=QueuePool, pool_size=1, max_overflow=0
    )
    instrument_engine(engine, alias="pooled")
    with engine.connect() as conn:  # open the pool's only connection up front
        conn.execute(text("SELECT 1"))
    held = threading.Event()
    release = threading.Event()

    def hog():
        with engine.connect():
            held.set()
            release.wait(2)

    t = threading.Thread(target=hog)
    t.start()
    held.wait(2)
    threading.Timer(0.05, release.set).start()
    recorder = make_recorder()
    with recorder.recording(method="GET", path="/x"):
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    t.join()
    [wait] = kinds(recorder.last_trace, "pool_wait")
    assert wait.duration_ms >= 40
    assert wait.data == {"db_alias": "pooled", "pool_size": 1, "checked_out": 1}
    assert wait.callsite is not None and wait.callsite.file == "tests/test_transactions.py"
    # an uncontended checkout from a warm pool is not recorded
    with recorder.recording(method="GET", path="/y"):
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    assert kinds(recorder.last_trace, "pool_wait") == []
    engine.dispose()


# ── detector ─────────────────────────────────────────────────────────────────────────


def _tx(ms: float, http: int = 0, start: float = 0.0) -> Span:
    return Span(
        id=new_id(),
        parent_id=None,
        kind="transaction",
        name="transaction default",
        start_ms=start,
        duration_ms=ms,
        callsite=None,
        data={"db_alias": "default", "outcome": "commit", "queries": 3, "http_out": http},
    )


def _trace(*spans: Span) -> Trace:
    return Trace(
        id=new_id(),
        framework="django",
        method="POST",
        path="/pay/1",
        route="/pay/<int:pk>",
        view=None,
        status=200,
        started_at=0.0,
        duration_ms=2000.0,
        spans=list(spans),
    )


def test_detector_registered():
    assert tx_detector.detect in REGISTRY


def test_long_transaction_thresholds():
    s = Settings()
    assert tx_detector.detect(_trace(_tx(150)), s) == []
    [warn] = tx_detector.detect(_trace(_tx(250)), s)
    assert warn.kind == "long_transaction" and warn.severity == "warning"
    assert warn.title == "Transaction open 250 ms on default"
    [crit] = tx_detector.detect(_trace(_tx(1200)), s)
    assert crit.severity == "critical"
    assert crit.id == warn.id  # same endpoint + alias


def test_external_in_transaction_without_http_spans_falls_back_to_counts():
    [issue] = tx_detector.detect(_trace(_tx(10, http=2)), Settings())
    assert issue.kind == "external_in_transaction"
    assert issue.title == "2 HTTP calls inside a transaction"
    assert issue.severity == "warning"
