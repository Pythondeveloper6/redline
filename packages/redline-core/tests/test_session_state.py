from __future__ import annotations

from types import SimpleNamespace

from redline_core import Recorder, Settings
from redline_core.session_state import checked_in, observe


def _span(vendor="mssql", span_id="s"):
    return SimpleNamespace(id=span_id, callsite=None, data={"vendor": vendor})


def test_mssql_options_identity_insert_and_values():
    info = {}
    span = _span()
    observe(
        info,
        "SET TRANSACTION ISOLATION LEVEL SNAPSHOT; "
        "SET IDENTITY_INSERT [dbo].[thing] ON; SET DATEFORMAT dmy; "
        "SET LOCK_TIMEOUT 5000",
        span,
    )
    changes = info["redline_session"]
    assert changes["TRANSACTION ISOLATION LEVEL"]["value"] == "SNAPSHOT"
    assert changes["IDENTITY_INSERT:DBO.THING"]["value"] == "ON"
    assert changes["DATEFORMAT"]["value"] == "DMY"
    assert changes["LOCK_TIMEOUT"]["value"] == "5000"
    observe(info, "SET IDENTITY_INSERT [dbo].[thing] OFF", span)
    assert "IDENTITY_INSERT:DBO.THING" not in info["redline_session"]


def test_expected_mssql_options_flag_at_checkin():
    recorder = Recorder(
        Settings(
            session_expected={
                "TRANSACTION ISOLATION LEVEL": "READ COMMITTED",
                "DATEFORMAT": "MDY",
            }
        )
    )
    with recorder.recording(method="GET", path="/"):
        info = {}
        observe(info, "SET TRANSACTION ISOLATION LEVEL SNAPSHOT; SET DATEFORMAT dmy", _span())
        checked_in(info, "default")
    evidence = next(
        span.data["session_state_leak"]
        for span in recorder.last_trace.spans
        if span.data.get("session_state_leak")
    )
    assert set(evidence) == {"TRANSACTION ISOLATION LEVEL", "DATEFORMAT"}


def test_session_context_respects_expected_keys_and_read_only():
    recorder = Recorder(Settings(session_expected_context=("tenant", "audit", "immutable")))
    with recorder.recording(method="GET", path="/"):
        info = {}
        observe(
            info,
            "EXEC sp_set_session_context @key = N'tenant', @value = @id, @read_only = 0",
            _span(),
        )
        observe(
            info,
            "EXEC sp_set_session_context @key = N'audit', @value = @id, @read_only = 1",
            _span(),
        )
        observe(
            info,
            "EXEC sp_set_session_context @key = N'unexpected', @value = @id, @read_only = 1",
            _span(),
        )
        checked_in(info, "default")
    evidence = next(
        span.data["session_state_leak"]
        for span in recorder.last_trace.spans
        if span.data.get("session_state_leak")
    )
    assert set(evidence) == {"CONTEXT:tenant", "CONTEXT:unexpected"}
    assert evidence["CONTEXT:tenant"]["read_only"] is False
    issue = next(i for i in recorder.last_trace.issues if i.kind == "session_state_leak")
    assert issue.severity == "info"


def test_session_context_null_clears_and_read_only_expected_is_quiet():
    recorder = Recorder(Settings(session_expected_context=("tenant",)))
    with recorder.recording(method="GET", path="/"):
        info = {}
        observe(
            info,
            "EXEC sp_set_session_context @key = N'tenant', @value = @id, @read_only = 1",
            _span(),
        )
        checked_in(info, "default")
        assert info["redline_session"]
        observe(
            info,
            "EXEC sp_set_session_context @key = N'tenant', @value = NULL",
            _span(),
        )
        assert not info["redline_session"]
    assert not [i for i in recorder.last_trace.issues if i.kind == "session_state_leak"]


def test_postgresql_set_config_local_and_reset():
    info = {}
    span = _span("postgresql")
    observe(info, "SELECT set_config('search_path', 'tenant, public', false)", span)
    assert "SEARCH_PATH" in info["redline_session"]
    observe(info, "SELECT set_config('statement_timeout', '5s', true)", span)
    assert "STATEMENT_TIMEOUT" not in info["redline_session"]
    observe(info, "SET LOCAL TIME ZONE 'UTC'", span)
    assert "TIMEZONE" not in info["redline_session"]
    observe(info, "RESET ALL", span)
    assert not info["redline_session"]


def test_mysql_session_and_user_variables():
    info = {}
    span = _span("mysql")
    observe(info, "SET SESSION sql_mode = 'ANSI'; SET @tenant_id = 42", span)
    changes = info["redline_session"]
    assert "SQL_MODE" in changes
    assert "USER_VAR:@tenant_id" in changes
