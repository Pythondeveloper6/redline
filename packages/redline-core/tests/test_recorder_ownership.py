"""Recorder ownership, concurrent registration and application failure isolation."""

from __future__ import annotations

import gc
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from redline_core import Recorder, Settings, configure, current, get_recorder, job
from redline_core.contrib import sqlalchemy as instrumentation
from redline_core.contrib.sqlalchemy import instrument_engine


def _sql(trace):
    return [span for span in trace.spans if span.kind == "sql"]


@pytest.mark.parametrize("observer_mode", ["explicit", "all"])
def test_owned_engine_only_records_for_another_recorder_when_it_opts_into_discovery(observer_mode):
    engine = create_engine("sqlite://")
    owner = Recorder(Settings(instrument_engines="explicit"))
    observer = Recorder(Settings(instrument_engines=observer_mode))
    instrument_engine(engine, "private_owner", recorder=owner)
    try:
        with observer.recording(method="GET", path="/observer"):
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT 41").scalar_one() == 41
                connection.invalidate(RuntimeError("observer connection lost"))
        trace = observer.last_trace
        if observer_mode == "explicit":
            assert trace.spans == []
            assert trace.query_count == 0
        else:
            assert trace.query_count == 1
            assert len(_sql(trace)) == 1
            assert _sql(trace)[0].data["auto_engine"] is True
            assert _sql(trace)[0].data["db_alias"] != "private_owner"
        assert owner.last_trace is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("observer_mode", ["explicit", "all"])
def test_flask_registration_does_not_register_an_engine_for_unrelated_recorders(
    store, observer_mode
):
    flask = pytest.importorskip("flask")
    adapter = pytest.importorskip("flask_redline")
    engine = create_engine("sqlite://")
    owner = Recorder(Settings(instrument_engines="explicit"), store)
    observer = Recorder(Settings(instrument_engines=observer_mode))
    adapter.Redline(flask.Flask(__name__), engine={"flask_db": engine}, recorder=owner)
    try:
        with observer.recording(method="GET", path="/other-app"):
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
        if observer_mode == "explicit":
            assert observer.last_trace.query_count == 0
            assert observer.last_trace.spans == []
        else:
            assert observer.last_trace.query_count == 1
            span = _sql(observer.last_trace)[0]
            assert span.data["auto_engine"] is True
            assert span.data["db_alias"] != "flask_db"
        with owner.recording(method="GET", path="/owner"):
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 2").scalar_one()
        assert len(_sql(owner.last_trace)) == 1
        assert _sql(owner.last_trace)[0].data["db_alias"] == "flask_db"
        assert _sql(owner.last_trace)[0].data["auto_engine"] is False
    finally:
        engine.dispose()


def test_same_engine_uses_the_active_owners_alias_in_parallel(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shared.db'}")
    owners = [Recorder(Settings(instrument_engines="explicit")) for _ in range(2)]
    aliases = ["application_a", "application_b"]
    for owner, alias in zip(owners, aliases, strict=True):
        instrument_engine(engine, alias, recorder=owner)
    barrier = threading.Barrier(2, timeout=30)

    def work(index):
        owner = owners[index]
        with owner.recording(method="GET", path=f"/app-{index}"):
            with engine.connect() as connection:
                barrier.wait()
                assert get_recorder() is owner
                for value in range(3):
                    assert connection.exec_driver_sql("SELECT ?", (value,)).scalar_one() == value
        return owner.last_trace

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            traces = list(workers.map(work, range(2)))
        for trace, alias in zip(traces, aliases, strict=True):
            assert trace.query_count == 3
            assert len(_sql(trace)) == 3
            assert {span.data["db_alias"] for span in _sql(trace)} == {alias}
            assert all(span.data["auto_engine"] is False for span in _sql(trace))
            transactions = [span for span in trace.spans if span.kind == "transaction"]
            assert len(transactions) == 1
            assert transactions[0].data["db_alias"] == alias
    finally:
        engine.dispose()


def test_concurrent_registration_does_not_duplicate_query_or_health_callbacks(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'registration.db'}")
    owner = Recorder(Settings(instrument_engines="explicit"))
    barrier = threading.Barrier(12, timeout=30)

    def register(_):
        barrier.wait()
        instrument_engine(engine, "shared", recorder=owner)

    try:
        with ThreadPoolExecutor(max_workers=12) as workers:
            list(workers.map(register, range(12)))
        with owner.recording(method="GET", path="/registered"):
            with engine.begin() as connection:
                for value in range(3):
                    assert connection.exec_driver_sql("SELECT ?", (value,)).scalar_one() == value
        trace = owner.last_trace
        assert trace.query_count == 3
        assert len(_sql(trace)) == 3
        assert [span.data["connection"]["statement_number"] for span in _sql(trace)] == [1, 2, 3]
        assert len([span for span in trace.spans if span.kind == "transaction"]) == 1
        with owner.recording(method="GET", path="/invalidated"):
            with engine.connect() as connection:
                connection.invalidate(RuntimeError("connection lost"))
        failures = [span for span in owner.last_trace.spans if span.data.get("connection_error")]
        assert len(failures) == 1
        assert owner.last_trace.tags["connection_errors"] == "1"
    finally:
        engine.dispose()


def test_registration_does_not_keep_a_recorder_alive():
    engine = create_engine("sqlite://")

    def register():
        owner = Recorder(Settings(instrument_engines="explicit"))
        instrument_engine(engine, "gone_owner", recorder=owner)
        return weakref.ref(owner)

    try:
        reference = register()
        gc.collect()
        assert reference() is None
        observer = Recorder(Settings(instrument_engines="explicit"))
        with observer.recording(method="GET", path="/after-owner-collection"):
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1").scalar_one()
        assert observer.last_trace.query_count == 0
    finally:
        engine.dispose()


def test_disposed_instrumented_engine_and_pool_can_be_collected():
    owner = Recorder(Settings(instrument_engines="explicit"))

    def use_engine():
        engine = create_engine("sqlite://")
        instrument_engine(engine, "temporary", recorder=owner)
        with owner.recording(method="GET", path="/temporary"):
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1").scalar_one()
        references = weakref.ref(engine), weakref.ref(engine.pool)
        engine.dispose()
        return references

    engine_reference, pool_reference = use_engine()
    gc.collect()
    assert engine_reference() is None
    assert pool_reference() is None


def test_implicit_nested_job_inherits_active_recorder_over_process_default(monkeypatch):
    from redline_core import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "_default", None)
    default = configure(Settings(capture_logs=False, instrument_engines="explicit"))
    owner = Recorder(Settings(instrument_engines="explicit"))
    with owner.recording(method="GET", path="/application-a") as outer:
        assert get_recorder() is owner
        with job("nested-work") as nested:
            assert nested.recorder is owner
            assert current() is nested
        assert current() is outer
        assert owner.last_trace.method == "TASK"
        assert default.last_trace is None
    assert owner.last_trace.method == "GET"
    assert get_recorder() is default


def test_explicit_nested_job_recorder_overrides_active_owner(monkeypatch):
    from redline_core import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "_default", None)
    default = configure(Settings(capture_logs=False))
    owner, worker = Recorder(), Recorder()
    with owner.recording(method="GET", path="/application-a") as outer:
        with job("separate-worker", recorder=worker) as nested:
            assert nested.recorder is worker
            assert current() is nested
        assert current() is outer
        assert worker.last_trace.method == "TASK"
        assert default.last_trace is None
    assert owner.last_trace.method == "GET"


@pytest.mark.parametrize("failure_site", ["detector", "store"])
def test_recording_failures_cannot_replace_the_application_exception(failure_site, monkeypatch):
    class FailedStore:
        def save(self, trace):
            raise RuntimeError("store unavailable")

    owner = Recorder(store=FailedStore() if failure_site == "store" else None)
    if failure_site == "detector":

        def fail(*args, **kwargs):
            raise RuntimeError("detector unavailable")

        monkeypatch.setattr(owner.detector, "detect", fail)
    application_error = ValueError("application validation failed")
    with pytest.raises(ValueError) as raised:
        with owner.recording(method="GET", path="/failed"):
            raise application_error
    assert raised.value is application_error
    assert current() is None


@pytest.mark.parametrize("query_fails", [False, True])
def test_query_recording_failure_does_not_change_database_behavior(monkeypatch, query_fails):
    engine = create_engine("sqlite://")
    owner = Recorder(Settings(instrument_engines="explicit"))
    instrument_engine(engine, recorder=owner)

    def fail(*args, **kwargs):
        raise RuntimeError("query recording unavailable")

    monkeypatch.setattr(instrumentation, "record_query", fail)
    try:
        with owner.recording(method="GET", path="/database"):
            with engine.connect() as connection:
                if query_fails:
                    with pytest.raises(OperationalError, match="missing_table"):
                        connection.exec_driver_sql("SELECT * FROM missing_table")
                else:
                    assert connection.exec_driver_sql("SELECT 17").scalar_one() == 17
    finally:
        engine.dispose()
