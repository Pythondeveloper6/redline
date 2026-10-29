"""Native cursor failures and privacy-preserving result counting."""

from types import SimpleNamespace

import pytest
from django.db import DatabaseError, connection, transaction
from django.test import override_settings

from django_redline.instrument import QueryRecorder
from django_redline.runtime import get_runtime

pytestmark = pytest.mark.django_db


def _start(rt):
    return rt.recorder.start_trace(method="GET", path="/caught-error/", framework="django")


def test_handled_native_sql_failure_is_reported():
    rt = get_runtime()
    builder = _start(rt)
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT private_column FROM missing_redline_table")
    trace = rt.recorder.finish_trace(builder, status=200)
    failed = [s for s in trace.spans if s.data.get("error")]
    assert len(failed) == 1
    assert failed[0].data["error"]["type"] == "OperationalError"
    assert failed[0].data["explainable"] is False
    assert trace.tags["sql_errors"] == "1"
    assert any(i.kind == "sql_error" for i in trace.issues)


@pytest.mark.parametrize("policy", [False, "reads_only"])
def test_failure_details_respect_sql_capture_policy(policy):
    with override_settings(REDLINE={"STORAGE_PATH": ":memory:", "CAPTURE_SQL_PARAMS": policy}):
        rt = get_runtime()
        builder = _start(rt)
        exc = DatabaseError("private-canary")

        def fail(*args):
            raise exc

        with pytest.raises(DatabaseError) as raised:
            QueryRecorder()(fail, "INSERT INTO hidden VALUES (%s)", ["secret"], False, {})
        trace = rt.recorder.finish_trace(builder, status=200)
    assert raised.value is exc
    span = next(s for s in trace.spans if s.kind == "sql")
    assert span.data["error"]["message"] == "[not captured]"
    assert span.data["params"] is None


def test_error_details_can_be_disabled_independently():
    with override_settings(REDLINE={"STORAGE_PATH": ":memory:", "CAPTURE_ERROR_DETAILS": False}):
        rt = get_runtime()
        builder = _start(rt)

        def fail(*args):
            raise DatabaseError("private-canary")

        with pytest.raises(DatabaseError):
            QueryRecorder()(fail, "SELECT %s", [1], False, {})
        trace = rt.recorder.finish_trace(builder, status=200)
    assert (
        next(s for s in trace.spans if s.kind == "sql").data["error"]["message"] == "[not captured]"
    )


def test_capture_off_still_counts_fetched_rows():
    with override_settings(REDLINE={"STORAGE_PATH": ":memory:", "CAPTURE_SQL_PARAMS": False}):
        rt = get_runtime()
        builder = _start(rt)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3")
            assert len(cursor.fetchmany(2)) == 2
            assert cursor.fetchone() == (3,)
            assert cursor.fetchone() is None
        trace = rt.recorder.finish_trace(builder, status=200)
    span = next(s for s in trace.spans if s.kind == "sql")
    assert span.data["rows"] == 3
    assert span.data["explainable"] is False
    assert span.data["params"] is None


def test_failure_count_survives_span_limit():
    with override_settings(REDLINE={"STORAGE_PATH": ":memory:", "MAX_SPANS_PER_TRACE": 1}):
        rt = get_runtime()
        builder = _start(rt)
        wrapper = QueryRecorder()
        wrapper(lambda *args: None, "SELECT 1", None, False, {})

        def fail(*args):
            raise DatabaseError("failure")

        with pytest.raises(DatabaseError):
            wrapper(fail, "SELECT 2", None, False, {})
        trace = rt.recorder.finish_trace(builder, status=200)
    assert trace.tags["sql_errors"] == "1"
    assert any(i.kind == "sql_error" and i.extra.get("incomplete_trace") for i in trace.issues)


def test_observation_does_not_consume_executemany_generator():
    rt = get_runtime()
    builder = _start(rt)

    def forbidden():
        raise AssertionError("observer consumed params")
        yield

    params = forbidden()
    assert (
        QueryRecorder()(lambda *args: "ok", "INSERT INTO t VALUES (%s)", params, True, {}) == "ok"
    )
    trace = rt.recorder.finish_trace(builder, status=200)
    span = next(s for s in trace.spans if s.kind == "sql")
    assert span.data["params_captured"] is False


def test_observer_error_preserves_original_database_error(monkeypatch):
    import django_redline.instrument as instrument

    rt = get_runtime()
    builder = _start(rt)

    def broken(*args, **kwargs):
        raise ValueError("observer failed")

    monkeypatch.setattr(instrument, "_record", broken)
    exc = DatabaseError("original")

    def fail(*args):
        raise exc

    with pytest.raises(DatabaseError) as raised:
        QueryRecorder()(fail, "SELECT 1", None, False, {})
    rt.recorder.finish_trace(builder, status=200)
    assert raised.value is exc


def test_django_comment_markers_use_public_paramstyle():
    rt = get_runtime()
    builder = _start(rt)
    QueryRecorder()(
        lambda *args: None,
        "SELECT 1 -- why? %s",
        None,
        False,
        {"connection": SimpleNamespace(vendor="microsoft", alias="default")},
    )
    trace = rt.recorder.finish_trace(builder, status=200)
    span = next(s for s in trace.spans if s.kind == "sql")
    assert span.data["vendor"] == "mssql"
    assert span.data["comment_placeholders"] == 1


def test_disabled_capture_clears_fallback_view_source():
    from django_redline.instrument import ViewFrame, _view

    with override_settings(REDLINE={"STORAGE_PATH": ":memory:", "CAPTURE_SQL_PARAMS": False}):
        rt = get_runtime()
        builder = _start(rt)
        token = _view.set(
            [ViewFrame(name="view", file=__file__, line=1, end_line=2, function="view")]
        )
        try:
            QueryRecorder()(lambda *args: None, "SELECT 'inline-private-value'", None, False, {})
        finally:
            _view.reset(token)
        trace = rt.recorder.finish_trace(builder, status=200)
    span = next(s for s in trace.spans if s.kind == "sql")
    assert span.callsite is not None
    assert span.callsite.code is None


def test_non_database_error_preserved_without_false_sql_failure():
    rt = get_runtime()
    builder = _start(rt)
    exc = RuntimeError("wrapper failed before SQL")

    def fail(*args):
        raise exc

    with pytest.raises(RuntimeError) as raised:
        QueryRecorder()(fail, "SELECT 1", None, False, {})
    trace = rt.recorder.finish_trace(builder, status=200)
    assert raised.value is exc
    assert not any(s.kind == "sql" for s in trace.spans)
    assert not trace.tags.get("sql_errors")
