"""Live integration matrix. Opt in only inside the disposable Compose environment."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from live_database_helpers import (
    IDENTITY_SQL,
    VENDORS,
    assert_database_name,
    assert_recordings,
    capture_settings,
    database_url,
    isolated_engine,
    prove_rollback,
    prove_session_state,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("REDLINE_TEST_COMPOSE") != "1",
    reason="Requires explicit disposable Compose integration environment",
)


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("framework", ("flask", "fastapi"))
def test_sync_http_database_recording(vendor, framework, tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    sentinel = uuid.uuid4().hex
    store_path = tmp_path / "capture.db"
    with isolated_engine(vendor) as (engine, table):

        def successful():
            with engine.connect() as connection:
                assert (
                    connection.execute(text("SELECT :payload"), {"payload": sentinel}).scalar_one()
                    == sentinel
                )
            return {"ok": True}

        def handled():
            try:
                with engine.connect() as connection:
                    connection.execute(text(f"SELECT missing_redline_column FROM {table.name}"))
            except SQLAlchemyError:
                return {"degraded": True}
            raise AssertionError("Missing-column query unexpectedly succeeded")

        if framework == "flask":
            from flask import Flask

            from flask_redline import Redline

            app = Flask("live-disposable")
            app.add_url_rule("/success", view_func=successful)
            app.add_url_rule("/handled", view_func=handled)
            extension = Redline(app, engine=engine, settings=capture_settings(store_path))
            try:
                with app.test_client() as client:
                    assert client.get("/success", buffered=True).json == {"ok": True}
                    assert client.get("/handled", buffered=True).json == {"degraded": True}
                assert_recordings(extension.store, store_path, sentinel)
            finally:
                extension.close()
        else:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient

            from fastapi_redline import install

            app = FastAPI()
            app.get("/success")(successful)
            app.get("/handled")(handled)
            extension = install(app, engine=engine, settings=capture_settings(store_path))
            try:
                with TestClient(app) as client:
                    assert client.get("/success").json() == {"ok": True}
                    assert client.get("/handled").json() == {"degraded": True}
                    assert_recordings(extension.store, store_path, sentinel)
            finally:
                extension.close()
        prove_rollback(engine, table)
        prove_session_state(engine, vendor)


@pytest.mark.parametrize("vendor", VENDORS)
def test_async_fastapi_http_database_recording_and_rollback(vendor, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select, text
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import create_async_engine

    from fastapi_redline import install

    sentinel = uuid.uuid4().hex
    store_path = tmp_path / "async-capture.db"
    with isolated_engine(vendor) as (_, table):
        engine = create_async_engine(database_url(vendor, asynchronous=True), hide_parameters=True)

        @asynccontextmanager
        async def lifespan(app):
            try:
                yield
            finally:
                await engine.dispose()

        app = FastAPI(lifespan=lifespan)

        @app.get("/success")
        async def successful():
            async with engine.connect() as connection:
                result = await connection.exec_driver_sql(IDENTITY_SQL[vendor])
                assert_database_name(result.scalar_one())
                result = await connection.execute(text("SELECT :payload"), {"payload": sentinel})
                assert result.scalar_one() == sentinel
            return {"ok": True}

        @app.get("/handled")
        async def handled():
            try:
                async with engine.connect() as connection:
                    result = await connection.exec_driver_sql(IDENTITY_SQL[vendor])
                    assert_database_name(result.scalar_one())
                    await connection.execute(
                        text(f"SELECT missing_redline_column FROM {table.name}")
                    )
            except SQLAlchemyError:
                return {"degraded": True}
            raise AssertionError("Missing-column query unexpectedly succeeded")

        @app.get("/rollback")
        async def rollback():
            async with engine.connect() as connection:
                transaction = await connection.begin()
                result = await connection.exec_driver_sql(IDENTITY_SQL[vendor])
                assert_database_name(result.scalar_one())
                await connection.execute(table.insert().values(id=1))
                result = await connection.execute(select(func.count()).select_from(table))
                assert result.scalar_one() == 1
                await transaction.rollback()
            async with engine.begin() as connection:
                result = await connection.execute(select(func.count()).select_from(table))
                assert result.scalar_one() == 0
                await connection.execute(table.insert().values(id=2))
            async with engine.begin() as connection:
                result = await connection.execute(select(table.c.id))
                assert result.scalars().all() == [2]
                await connection.execute(table.delete())
            return {"rollback": "verified", "next_transaction": "committed"}

        extension = install(app, engine=engine, settings=capture_settings(store_path))
        try:
            with TestClient(app) as client:
                assert client.get("/success").json() == {"ok": True}
                assert client.get("/handled").json() == {"degraded": True}
                assert client.get("/rollback").json() == {
                    "rollback": "verified",
                    "next_transaction": "committed",
                }
                assert_recordings(extension.store, store_path, sentinel)
        finally:
            extension.close()


@pytest.mark.parametrize("vendor", VENDORS)
def test_django_real_backend_http_and_transaction_isolation(vendor, tmp_path):
    helper = Path(__file__).with_name("live_database_helpers.py")
    # Database access and settings live in a new interpreter, separate from pytest-django.
    result = subprocess.run(
        [sys.executable, str(helper), vendor, str(tmp_path / "django-capture.db")],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": ""},
    )
    # Never print subprocess driver free-text errors or DSNs into the test report.
    assert result.returncode == 0, (
        f"Django disposable integration failed for {vendor} (exit {result.returncode})"
    )
    assert '"django": "passed"' in result.stdout
