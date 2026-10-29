"""Observer failures preserve SQLite behavior and never log sensitive error details."""

import sqlite3

import pytest
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, declarative_base, mapped_column

from redline_core import Recorder, Settings
from redline_core.contrib import sqlalchemy as hooks

SENTINEL = "PRIVATE_OBSERVER_DIAGNOSTIC_VALUE"


def fail(*args, **kwargs):
    raise RuntimeError(SENTINEL)


@pytest.mark.parametrize(
    "target", ["_binding", "begin_transaction", "end_transaction", "record_span"]
)
def test_pool_transaction_and_binding_capture_failures_preserve_results(
    target, monkeypatch, caplog
):
    owner = Recorder(Settings(instrument_engines="explicit"))
    engine = create_engine("sqlite://")
    hooks.instrument_engine(engine, recorder=owner)
    monkeypatch.setattr(hooks, "POOL_WAIT_MIN", -1)
    monkeypatch.setattr(hooks, target, fail)
    try:
        with owner.recording(method="GET", path="/safe") as builder:
            with engine.begin() as connection:
                assert connection.exec_driver_sql("SELECT 17").scalar_one() == 17
            assert builder.capture_errors >= 1
        assert SENTINEL not in caplog.text
        assert "RuntimeError" in caplog.text
    finally:
        engine.dispose()


def test_connect_observer_failure_preserves_connection(monkeypatch, caplog):
    from redline_core import connection_health

    owner = Recorder(Settings(instrument_engines="explicit"))
    engine = create_engine("sqlite://")
    hooks.instrument_engine(engine, recorder=owner)
    monkeypatch.setattr(connection_health, "connected", fail)
    try:
        with owner.recording(method="GET", path="/safe") as builder:
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT 17").scalar_one() == 17
            assert builder.capture_errors >= 1
        assert SENTINEL not in caplog.text
    finally:
        engine.dispose()


def test_checkin_observer_failure_preserves_close(monkeypatch, caplog):
    from redline_core import session_state

    owner = Recorder(Settings(instrument_engines="explicit"))
    engine = create_engine("sqlite://")
    hooks.instrument_engine(engine, recorder=owner)
    monkeypatch.setattr(session_state, "checked_in", fail)
    try:
        with owner.recording(method="GET", path="/safe") as builder:
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT 17").scalar_one() == 17
            assert builder.capture_errors >= 1
        assert SENTINEL not in caplog.text
    finally:
        engine.dispose()


def test_invalidation_observer_failure_does_not_change_connection_cleanup(monkeypatch, caplog):
    owner = Recorder(Settings(instrument_engines="explicit"))
    engine = create_engine("sqlite://")
    hooks.instrument_engine(engine, recorder=owner)
    monkeypatch.setattr(hooks, "record_span", fail)
    try:
        with owner.recording(method="GET", path="/safe") as builder:
            with engine.connect() as connection:
                connection.invalidate(RuntimeError("driver disconnect"))
            assert builder.capture_errors >= 1
        assert SENTINEL not in caplog.text
    finally:
        engine.dispose()


def test_orm_observer_failure_preserves_loaded_objects(monkeypatch, caplog):
    from redline_core import orm

    base = declarative_base()

    class Item(base):
        __tablename__ = "items"
        id = mapped_column(Integer, primary_key=True)

    owner = Recorder(Settings(instrument_engines="explicit"))
    engine = create_engine("sqlite://")
    base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO items (id) VALUES (1)")
    hooks.instrument_engine(engine, recorder=owner)
    monkeypatch.setattr(orm, "note_instance", fail)
    try:
        with owner.recording(method="GET", path="/safe") as builder:
            with Session(engine) as session:
                assert session.scalars(select(Item)).one().id == 1
            assert builder.capture_errors >= 1
        assert SENTINEL not in caplog.text
    finally:
        engine.dispose()


def test_real_checkout_error_remains_original_and_creator_runs_once():
    calls = []
    error = sqlite3.OperationalError("original connection error")

    def creator():
        calls.append(1)
        raise error

    engine = create_engine("sqlite://", creator=creator)
    owner = Recorder(Settings(instrument_engines="explicit"))
    hooks.instrument_engine(engine, recorder=owner)
    try:
        with owner.recording(method="GET", path="/safe"):
            with pytest.raises(OperationalError) as raised:
                engine.connect()
            assert raised.value.orig is error
        assert calls == [1]
    finally:
        engine.dispose()
