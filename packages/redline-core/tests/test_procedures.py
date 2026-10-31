from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event

from redline_core import Recorder, Settings
from redline_core.conf import DEFAULT_IGNORE_SQL
from redline_core.contrib.sqlalchemy import instrument_engine
from redline_core.sql import fingerprint, is_ignored, procedure_name, query_kind


@pytest.mark.parametrize(
    "sql,name",
    [
        ("EXEC [dbo].[usp_orders] @id = 5, @tag = N'x'", "dbo.usp_orders"),
        ("execute dbo.usp_orders 7", "dbo.usp_orders"),
        ("EXEC @rc = [sales].[usp_total] @a = 1", "sales.usp_total"),
        ("CALL orders_for(1, 'a')", "orders_for"),
        ("CALL `shop`.`orders_for`(?)", "shop.orders_for"),
        ("{CALL orders_for(?, ?)}", "orders_for"),
        ("-- note\nEXEC dbo.p 1", "dbo.p"),
    ],
)
def test_procedure_name(sql, name):
    assert procedure_name(sql) == name
    assert query_kind(sql) == "procedure"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "EXEC sp_executesql N'select 1'",
        "EXEC('select 1')",
        "SET NOCOUNT ON; EXEC dbo.p 1",
        "UPDATE t SET a = 1",
    ],
)
def test_not_a_procedure(sql):
    assert procedure_name(sql) is None
    assert query_kind(sql) != "procedure"


def test_fingerprint_groups_by_procedure_not_arguments():
    a = fingerprint("EXEC [dbo].[usp_orders] @id = 1")
    b = fingerprint("EXECUTE dbo.usp_orders @id = 2, @status = 'x'")
    assert a == b == "EXEC dbo.usp_orders"
    assert fingerprint("CALL orders_for(1)") == fingerprint("CALL orders_for(2, 'x')")
    assert fingerprint("CALL a(1)") != fingerprint("CALL b(1)")


def test_default_ignore_list_matches_session_housekeeping_only():
    d = DEFAULT_IGNORE_SQL
    assert is_ignored("EXEC sys.sp_set_session_context @key = N'uid', @value = 5", d)
    assert is_ignored("exec sp_set_session_context 'k', 'v'", d)
    assert is_ignored("SET search_path TO app", d)
    assert is_ignored("SET NOCOUNT ON; SET XACT_ABORT ON", d)
    assert not is_ignored("SET NOCOUNT ON; SELECT * FROM t", d)
    assert not is_ignored("EXEC dbo.usp_orders 1", d)
    assert not is_ignored("SELECT 1", d)
    assert not is_ignored("SET NAMES utf8", ())


def _run(settings: Settings, statements: list[str]):
    engine = create_engine("sqlite://")
    instrument_engine(engine)
    recorder = Recorder(settings)

    @event.listens_for(engine, "do_execute", retval=True)
    def fake(cursor, statement, parameters, context):
        return True  # sqlite has no procedures: skip the real execution

    try:
        with recorder.recording(method="GET", path="/p"):
            with engine.connect() as conn:
                for sql in statements:
                    conn.exec_driver_sql(sql)
        return recorder.last_trace
    finally:
        engine.dispose()


def test_procedure_spans_carry_kind_and_name():
    trace = _run(Settings(), ["EXEC dbo.usp_orders 1", "SELECT 1"])
    spans = [s for s in trace.spans if s.kind == "sql"]
    proc = next(s for s in spans if s.data.get("procedure"))
    assert proc.data["query_kind"] == "procedure"
    assert proc.data["procedure"] == "dbo.usp_orders"
    assert proc.data["fingerprint"] == "EXEC dbo.usp_orders"
    assert proc.data["explainable"] is False


def test_session_housekeeping_is_ignored_by_default():
    trace = _run(
        Settings(),
        ["EXEC sys.sp_set_session_context @key = N'uid', @value = 5", "SET NOCOUNT ON", "SELECT 1"],
    )
    assert trace.query_count == 1


def test_ignore_list_is_configurable():
    statements = ["EXEC sys.sp_set_session_context @key = N'uid', @value = 5", "SELECT 1"]
    assert _run(Settings(ignore_sql=()), statements).query_count == 2
    custom = _run(Settings(ignore_sql=(r"^SELECT\s",)), statements)
    assert custom.query_count == 1
    assert next(s for s in custom.spans if s.kind == "sql").data["query_kind"] == "procedure"


def test_invalid_ignore_pattern_is_rejected():
    from redline_core.conf import SettingsError

    with pytest.raises(SettingsError):
        Settings(ignore_sql=("(",))


def test_n_plus_one_groups_procedure_calls_across_arguments():
    calls = [f"EXEC dbo.usp_item_for_order @order_id = {i}" for i in range(6)]
    engine = create_engine("sqlite://")
    instrument_engine(engine)
    recorder = Recorder(Settings(n_plus_one_threshold=3))

    @event.listens_for(engine, "do_execute", retval=True)
    def fake(cursor, statement, parameters, context):
        return True  # sqlite has no procedures: skip the real execution

    with recorder.recording(method="GET", path="/loop"):
        with engine.connect() as conn:
            for sql in calls:  # one call site, different arguments
                conn.exec_driver_sql(sql)
    engine.dispose()
    issues = [i for i in recorder.last_trace.issues if i.kind == "n_plus_one"]
    assert len(issues) == 1 and issues[0].count == 6
    assert "procedure dbo.usp_item_for_order" in issues[0].title


def test_duplicate_procedure_calls_are_info_not_warning():
    engine = create_engine("sqlite://")
    instrument_engine(engine)
    recorder = Recorder(Settings(duplicate_threshold=2, n_plus_one_threshold=50))

    @event.listens_for(engine, "do_execute", retval=True)
    def fake(cursor, statement, parameters, context):
        return True  # sqlite has no procedures: skip the real execution

    with recorder.recording(method="GET", path="/dup"):
        with engine.connect() as conn:
            for _ in range(6):
                conn.exec_driver_sql("EXEC dbo.usp_refresh 1")
    engine.dispose()
    dup = [i for i in recorder.last_trace.issues if i.kind == "duplicate_query"]
    assert dup and all(i.severity == "info" for i in dup)
