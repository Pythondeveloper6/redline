"""MySQL-specific live checks: EXPLAIN, index advice, dead connections, runtime engines."""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest
from live_database_helpers import database_url, isolated_engine

pytestmark = pytest.mark.skipif(
    os.environ.get("REDLINE_TEST_COMPOSE") != "1",
    reason="Requires explicit disposable Compose integration environment",
)

SYNC_DRIVERS = ("pymysql",)
ASYNC_DRIVERS = ("aiomysql", "asyncmy")


def _filled_engine(engine, table_name):
    from sqlalchemy import Column, Integer, MetaData, String, Table

    table = Table(
        table_name,
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("tag", String(32)),
    )
    table.create(engine)
    with engine.begin() as connection:
        connection.execute(table.insert(), [{"id": i, "tag": f"t{i % 50}"} for i in range(1, 3001)])
    return table


def test_mysql_explain_and_index_advice():
    from sqlalchemy import text

    from redline_core import Recorder, Settings
    from redline_core.contrib.sqlalchemy import build_executor, instrument_engine
    from redline_core.explain import run_explain
    from redline_core.index_advisor import advise
    from redline_core.storage import SQLiteStore

    with isolated_engine("mysql") as (engine, probe):
        name = "redline_probe_" + uuid.uuid4().hex[:16]
        table = _filled_engine(engine, name)
        try:
            executor = build_executor(engine, "mysql", lambda: None)
            sql = f"SELECT id FROM {name} WHERE tag = 't1'"
            result = run_explain(sql, None, "mysql", executor)
            assert result.vendor == "mysql" and result.analyzed is False and result.tree
            assert any("Full table scan" in f.title for f in result.findings)
            assert any(name in s and "(tag)" in s for s in result.suggested_indexes)

            instrument_engine(engine)
            with tempfile.TemporaryDirectory() as tmp:
                store = SQLiteStore(Path(tmp) / "mysql.db", background=False)
                recorder = Recorder(Settings(access="local", capture_sql_params=True), store)
                for i in range(30):
                    with recorder.recording(method="GET", path="/tagged"):
                        with engine.connect() as connection:
                            connection.execute(
                                text(f"SELECT id FROM {name} WHERE tag = :t"), {"t": f"t{i}"}
                            ).all()
                assert store.flush(10)
                advice = advise(
                    store.sql_workload(3600),
                    get_trace=store.get,
                    executor_factory=lambda db, vendor: executor,
                )
                store.close()
            candidates = [c for c in advice["candidates"] if c["table"] == name]
            assert candidates and candidates[0]["columns"] == ["tag"]
            assert candidates[0]["vendor"] == "mysql" and candidates[0]["verified"] is True
            assert candidates[0]["create_sql"].startswith("CREATE INDEX")
        finally:
            table.drop(engine, checkfirst=True)
            probe.drop(engine, checkfirst=True)


@pytest.mark.parametrize("driver", SYNC_DRIVERS)
def test_mysql_connection_killed_is_recorded_and_pool_recovers(driver):
    from sqlalchemy.exc import DBAPIError

    from redline_core import Recorder, Settings
    from redline_core.contrib.sqlalchemy import instrument_engine

    with isolated_engine("mysql", driver=driver, pool_pre_ping=False) as (engine, _):
        instrument_engine(engine, "mysql")
        recorder = Recorder(Settings(access="local"))
        with recorder.recording(method="GET", path="/killed"):
            with engine.connect() as victim:
                connection_id = victim.exec_driver_sql("SELECT CONNECTION_ID()").scalar_one()
                with engine.connect() as admin:
                    admin.exec_driver_sql(f"KILL {int(connection_id)}")
                with pytest.raises(DBAPIError) as caught:
                    victim.exec_driver_sql("SELECT 1")
                assert caught.value.connection_invalidated
        trace = recorder.last_trace
        assert "connection_failure" in trace.issue_kinds or trace.sql_errors >= 1
        # The invalidated connection is discarded: the next checkout opens a fresh one.
        with engine.connect() as fresh:
            assert fresh.exec_driver_sql("SELECT 1").scalar_one() == 1
            assert fresh.exec_driver_sql("SELECT CONNECTION_ID()").scalar_one() != connection_id


def test_mysql_pre_ping_replaces_a_killed_pooled_connection():
    from redline_core import Recorder, Settings
    from redline_core.contrib.sqlalchemy import instrument_engine

    with isolated_engine("mysql", pool_pre_ping=True, pool_size=1, max_overflow=0) as (engine, _):
        instrument_engine(engine, "mysql")
        with engine.connect() as first:
            old_id = first.exec_driver_sql("SELECT CONNECTION_ID()").scalar_one()
        # The connection sits idle in the pool; kill it from outside the pool.
        killer = database_url("mysql")
        from sqlalchemy import create_engine

        admin = create_engine(killer)
        try:
            with admin.begin() as connection:
                connection.exec_driver_sql(f"KILL {int(old_id)}")
        finally:
            admin.dispose()
        recorder = Recorder(Settings(access="local"))
        with recorder.recording(method="GET", path="/pooled"):
            with engine.connect() as second:
                assert second.exec_driver_sql("SELECT CONNECTION_ID()").scalar_one() != old_id
        assert recorder.last_trace.query_count == 1


@pytest.mark.parametrize("driver", ASYNC_DRIVERS)
def test_mysql_async_drivers_are_recorded(driver):
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from redline_core import Recorder, Settings
    from redline_core.contrib.sqlalchemy import instrument_engine

    async def scenario():
        engine = create_async_engine(
            database_url("mysql", asynchronous=True, driver=driver), hide_parameters=True
        )
        instrument_engine(engine, "async_mysql")
        recorder = Recorder(Settings(access="local"))
        try:
            with recorder.recording(method="GET", path="/async"):
                async with engine.connect() as connection:
                    result = await connection.exec_driver_sql("SELECT DATABASE()")
                    assert result.scalar_one().startswith("redline_test_")
            return recorder.last_trace
        finally:
            await engine.dispose()

    trace = asyncio.run(scenario())
    span = next(s for s in trace.spans if s.kind == "sql")
    assert span.data["vendor"] == "mysql" and span.data["db_alias"] == "async_mysql"


@pytest.mark.parametrize("driver", SYNC_DRIVERS)
def test_mysql_runtime_engines_are_discovered_automatically(driver):
    from sqlalchemy import create_engine

    from redline_core import Recorder, Settings

    url = database_url("mysql", driver=driver)
    recorder = Recorder(Settings(access="local", instrument_engines="all"))
    engines = [create_engine(url, connect_args={"connect_timeout": 5 + i}) for i in range(3)]
    try:
        with recorder.recording(method="GET", path="/per-user"):
            for engine in engines:
                with engine.connect() as connection:
                    connection.exec_driver_sql("SELECT 1")
        spans = [s for s in recorder.last_trace.spans if s.kind == "sql"]
        assert len(spans) == 3 and all(s.data["auto_engine"] for s in spans)
        assert len({s.data["db_alias"] for s in spans}) == 3
    finally:
        for engine in engines:
            engine.dispose()
