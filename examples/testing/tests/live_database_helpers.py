"""Disposable live-database integration helpers; DSNs are never reported."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager

VENDORS = ("mysql", "mssql", "postgres")
DRIVERS = {"mysql": "mysql+pymysql", "mssql": "mssql+pyodbc", "postgres": "postgresql+psycopg"}
ASYNC_DRIVERS = {
    "mysql": "mysql+aiomysql",
    "mssql": "mssql+aioodbc",
    "postgres": "postgresql+asyncpg",
}
#: Extra drivers accepted per (vendor, kind); the DSN is the vendor's own with the driver swapped.
ALT_DRIVERS = {
    ("mysql", True): ("aiomysql", "asyncmy"),
    ("mysql", False): ("pymysql",),
}
IDENTITY_SQL = {
    "mysql": "SELECT DATABASE()",
    "mssql": "SELECT DB_NAME()",
    "postgres": "SELECT current_database()",
}


def require_compose():
    if os.environ.get("REDLINE_TEST_COMPOSE") != "1":
        raise RuntimeError("Disposable Compose integration consent is required")


def database_url(vendor, *, asynchronous=False, driver=None):
    from sqlalchemy.engine import make_url

    require_compose()
    suffix = "_ASYNC_DSN" if asynchronous else "_DSN"
    name = "REDLINE_TEST_" + vendor.upper() + suffix
    if vendor == "postgres" and not asynchronous:
        name = "REDLINE_TEST_POSTGRES_SQLALCHEMY_DSN"
    raw = os.environ.get(name)
    if not raw:
        raise RuntimeError("Required disposable test DSN is missing: " + name)
    url = make_url(raw)
    expected = ASYNC_DRIVERS[vendor] if asynchronous else DRIVERS[vendor]
    if driver is not None:
        if driver not in ALT_DRIVERS.get((vendor, asynchronous), ()):
            raise RuntimeError("Unsupported driver override")
        url = url.set(drivername=f"{vendor}+{driver}")
        expected = url.drivername
    if url.drivername != expected or not (url.database or "").startswith("redline_test_"):
        raise RuntimeError("Refusing unexpected driver or non-disposable database")
    return url


def assert_database_name(name):
    if type(name) is not str or not name.startswith("redline_test_"):
        raise RuntimeError("Server did not confirm a disposable database")


def capture_settings(path):
    return {
        "preset": "production",
        "access": "local",
        "sample_rate": 1.0,
        "storage_path": str(path),
        "capture_sql_params": False,
    }


def stored_trace(store, path):
    assert store.flush(10)
    items = [item for item in store.list(limit=100).items if item["path"] == path]
    assert len(items) == 1
    return store.get(items[0]["id"])


def assert_private_store(path, sentinel):
    with sqlite3.connect(path) as connection:
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            quoted = '"' + table.replace('"', '""') + '"'
            for row in connection.execute(f"SELECT * FROM {quoted}"):
                assert sentinel not in repr(row), "Private parameter reached trace storage"


def assert_recordings(store, path, sentinel):
    successful = stored_trace(store, "/success")
    failed = stored_trace(store, "/handled")
    assert successful.status == failed.status == 200
    assert successful.query_count >= 1
    assert any(issue.kind == "sql_error" for issue in failed.issues)
    assert failed.tags.get("sql_errors") == "1"
    assert all(
        span.data.get("params") is None
        for trace in (successful, failed)
        for span in trace.sql_spans()
    )
    assert_private_store(path, sentinel)


@contextmanager
def isolated_engine(vendor, *, driver=None, **engine_kwargs):
    from sqlalchemy import Column, Integer, MetaData, Table, create_engine

    engine_kwargs.setdefault("pool_pre_ping", True)
    engine = create_engine(
        database_url(vendor, driver=driver), hide_parameters=True, **engine_kwargs
    )
    table = Table(
        "redline_probe_" + uuid.uuid4().hex[:16],
        MetaData(),
        Column("id", Integer, primary_key=True),
    )
    verified = False
    try:
        with engine.connect() as connection:
            assert_database_name(connection.exec_driver_sql(IDENTITY_SQL[vendor]).scalar_one())
        verified = True
        table.create(engine)
        yield engine, table
    finally:
        if verified:
            table.drop(engine, checkfirst=True)
        engine.dispose()


def prove_rollback(engine, table):
    from sqlalchemy import func, select

    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(table.insert().values(id=1))
        assert connection.execute(select(func.count()).select_from(table)).scalar_one() == 1
        transaction.rollback()
    # A new pooled checkout observes no row and can complete an independent transaction.
    with engine.begin() as connection:
        assert connection.execute(select(func.count()).select_from(table)).scalar_one() == 0
        connection.execute(table.insert().values(id=2))
    with engine.begin() as connection:
        assert connection.execute(select(table.c.id)).scalars().all() == [2]
        connection.execute(table.delete())


def prove_session_state(engine, vendor):
    # Restore settings on the SAME connection before it returns to the shared pool.
    if vendor == "mssql":
        with engine.connect() as connection:
            old = connection.exec_driver_sql("SELECT @@OPTIONS & 512").scalar_one()
            try:
                connection.exec_driver_sql("SET NOCOUNT ON")
                assert connection.exec_driver_sql("SELECT @@OPTIONS & 512").scalar_one() == 512
            finally:
                connection.exec_driver_sql("SET NOCOUNT " + ("ON" if old else "OFF"))
            assert connection.exec_driver_sql("SELECT @@OPTIONS & 512").scalar_one() == old
    elif vendor == "mysql":
        with engine.connect() as connection:
            old = int(connection.exec_driver_sql("SELECT @@SESSION.sql_safe_updates").scalar_one())
            try:
                connection.exec_driver_sql("SET SESSION sql_safe_updates = " + str(1 - old))
                assert (
                    int(
                        connection.exec_driver_sql("SELECT @@SESSION.sql_safe_updates").scalar_one()
                    )
                    == 1 - old
                )
            finally:
                connection.exec_driver_sql("SET SESSION sql_safe_updates = " + str(old))
            assert (
                int(connection.exec_driver_sql("SELECT @@SESSION.sql_safe_updates").scalar_one())
                == old
            )
    else:
        with engine.connect() as connection:
            old = connection.exec_driver_sql("SHOW application_name").scalar_one()
            connection.exec_driver_sql("SET LOCAL application_name = 'redline_disposable_probe'")
            assert (
                connection.exec_driver_sql("SHOW application_name").scalar_one()
                == "redline_disposable_probe"
            )
            connection.rollback()
            assert connection.exec_driver_sql("SHOW application_name").scalar_one() == old


def run_django(vendor, store_path):
    """Called in a fresh interpreter, with real Django backend connections and middleware."""
    import django
    from django.conf import settings

    url = database_url(vendor)
    backend = {
        "mysql": "django.db.backends.mysql",
        "mssql": "mssql",
        "postgres": "django.db.backends.postgresql",
    }[vendor]
    options = {}
    if vendor == "mssql":
        options["driver"] = url.query.get("driver", "ODBC Driver 18 for SQL Server")
        if url.query.get("TrustServerCertificate"):
            options["extra_params"] = (
                "TrustServerCertificate=" + url.query["TrustServerCertificate"]
            )
    settings.configure(
        SECRET_KEY="disposable-integration",
        DEBUG=False,
        ALLOWED_HOSTS=["testserver"],
        ROOT_URLCONF=__name__,
        INSTALLED_APPS=["django_redline"],
        MIDDLEWARE=["django_redline.middleware.RedlineMiddleware"],
        DATABASES={
            "default": {
                "ENGINE": backend,
                "NAME": url.database,
                "USER": url.username,
                "PASSWORD": url.password,
                "HOST": url.host,
                "PORT": str(url.port or ""),
                "OPTIONS": options,
            }
        },
        REDLINE=capture_settings(store_path),
    )
    django.setup()
    from django.db import connection, transaction
    from django.http import JsonResponse
    from django.test import Client
    from django.urls import path

    from django_redline.runtime import get_runtime, reset_runtime

    sentinel = uuid.uuid4().hex
    table_name = "redline_probe_" + uuid.uuid4().hex[:16]
    quoted = connection.ops.quote_name(table_name)
    created = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(IDENTITY_SQL[vendor])
            assert_database_name(cursor.fetchone()[0])
            cursor.execute(f"CREATE TABLE {quoted} (id INTEGER PRIMARY KEY)")
            created = True

        def successful(request):
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s", [sentinel])
                assert cursor.fetchone()[0] == sentinel
            return JsonResponse({"ok": True})

        def handled(request):
            from django.db import DatabaseError

            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(f"SELECT missing_redline_column FROM {quoted}")
            except DatabaseError:
                return JsonResponse({"degraded": True})
            raise AssertionError("Missing-column query unexpectedly succeeded")

        globals()["urlpatterns"] = [path("success", successful), path("handled", handled)]
        client = Client()
        assert client.get("/success").status_code == 200
        assert client.get("/handled").json() == {"degraded": True}
        runtime = get_runtime()
        assert_recordings(runtime.store, store_path, sentinel)
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f"INSERT INTO {quoted} (id) VALUES (%s)", [1])
            transaction.set_rollback(True)
        connection.close()
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {quoted}")
            assert cursor.fetchone()[0] == 0
        print(json.dumps({"vendor": vendor, "django": "passed", "rollback": "verified"}))
    finally:
        if created:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE {quoted}")
        connection.close()
        reset_runtime()


if __name__ == "__main__":
    import sys

    try:
        run_django(sys.argv[1], sys.argv[2])
    except Exception as error:
        # Driver free-text errors may include credentials or connection strings.
        print(json.dumps({"error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
