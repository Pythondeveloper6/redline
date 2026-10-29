"""Explicit, opt-in integration probe for an isolated MySQL or SQL Server test database."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

ENVIRON = {"mysql": "REDLINE_DEMO_MYSQL_DSN", "mssql": "REDLINE_DEMO_MSSQL_DSN"}
DRIVERS = {"mysql": "mysql+pymysql", "mssql": "mssql+pyodbc"}


class ProbeRefused(ValueError):
    """The database cannot be established as an explicitly selected test target."""


def guard_database_name(name):
    if not isinstance(name, str) or not name.lower().startswith("redline_test_"):
        raise ProbeRefused("actual_database_is_not_a_named_test_database")


def validate_url(vendor, dsn):
    from sqlalchemy.engine import make_url

    try:
        url = make_url(dsn)
    except Exception:
        raise ProbeRefused("invalid_test_dsn") from None
    if url.drivername != DRIVERS[vendor]:
        raise ProbeRefused("driver_does_not_match_probe")
    # A connection using a named ODBC DSN may not expose its database in the URL.
    # Every connection is also checked with the server's actual database-name query.
    if url.database:
        guard_database_name(url.database)
    return url


def probe(vendor, dsn, *, store_path, plans=False):
    from sqlalchemy import create_engine
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.pool import NullPool

    from redline_core import Recorder
    from redline_core.config import resolve_settings
    from redline_core.contrib.sqlalchemy import build_executor, instrument_engine
    from redline_core.explain import ExplainError, run_explain
    from redline_core.storage import SQLiteStore

    url = validate_url(vendor, dsn)
    settings = resolve_settings(
        {
            "preset": "production",
            "access": "local",
            "storage_path": str(store_path),
            "capture_request_body": False,
            "capture_response_body": False,
            "capture_locals": False,
            "capture_logs": False,
            "capture_sql_params": False,
            "sample_rate": 1.0,
        },
        read_file=False,
        environ={},
    ).settings
    store = SQLiteStore.from_settings(settings)
    recorder = Recorder(settings, store)
    engine = None
    report = {"vendor": vendor, "driver": DRIVERS[vendor], "database_guard_verified": False}
    table = "#redline_probe" if vendor == "mssql" else "redline_probe"
    create = "CREATE TABLE" if vendor == "mssql" else "CREATE TEMPORARY TABLE"
    try:
        engine = create_engine(url, poolclass=NullPool, hide_parameters=True, echo=False)
        instrument_engine(engine)
        with engine.connect() as conn:
            actual = conn.exec_driver_sql(
                "SELECT DB_NAME()" if vendor == "mssql" else "SELECT DATABASE()"
            ).scalar_one()
            guard_database_name(actual)
            report["database_guard_verified"] = True
            conn.rollback()
            with recorder.recording(method="TASK", path="demo.driver-probe"):
                try:
                    conn.exec_driver_sql(f"{create} {table} (id INTEGER NOT NULL)")
                    conn.exec_driver_sql(f"INSERT INTO {table} VALUES (1)")
                    assert conn.exec_driver_sql(f"SELECT id FROM {table}").scalar_one() == 1
                    try:
                        conn.exec_driver_sql(f"SELECT unavailable_demo_column FROM {table}")
                    except DBAPIError:
                        pass
                    else:
                        raise AssertionError("expected_missing_column_error_was_not_raised")
                finally:
                    conn.rollback()
        trace = recorder.last_trace
        failures = [s for s in trace.spans if s.kind == "sql" and s.data.get("error")]
        assert failures and failures[0].data["vendor"] == vendor
        assert failures[0].data["error"]["category"] == "schema"
        assert "sql_error" in trace.issue_kinds
        assert all(s.data.get("params") is None for s in trace.spans if s.kind == "sql")
        report.update(
            {
                "trace_id": trace.id,
                "sql_error_recorded": True,
                "error_category": "schema",
                "rollback_requested": True,
                "only_session_temporary_tables_used": True,
            }
        )
        if plans:
            try:
                plan = run_explain(
                    "SELECT 1", None, vendor, build_executor(engine, vendor, lambda: None)
                )
                report["estimated_plan"] = {"status": "available", "vendor": plan.vendor}
            except ExplainError as exc:
                report["estimated_plan"] = {"status": "unavailable", "error_code": exc.code}
        store.flush()
        return report
    finally:
        if engine is not None:
            engine.dispose()  # Owned physical sessions contain only temporary business-data tables.
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor", choices=tuple(ENVIRON), required=True)
    parser.add_argument(
        "--disposable", action="store_true", help="confirm the target is disposable"
    )
    parser.add_argument(
        "--plans", action="store_true", help="also request an estimated SELECT 1 plan"
    )
    parser.add_argument(
        "--store", type=Path, help="optional private trace path outside the checkout"
    )
    args = parser.parse_args()
    if not args.disposable:
        print(json.dumps({"refused": "disposable_confirmation_required"}))
        return 2
    dsn = os.environ.get(ENVIRON[args.vendor])
    if not dsn:
        print(json.dumps({"refused": "missing_test_dsn", "variable": ENVIRON[args.vendor]}))
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="redline-driver-probe-") as directory:
            report = probe(
                args.vendor,
                dsn,
                store_path=args.store or Path(directory) / "traces.db",
                plans=args.plans,
            )
        print(json.dumps(report))
        return 1 if report.get("estimated_plan", {}).get("status") == "unavailable" else 0
    except ProbeRefused as exc:
        print(json.dumps({"refused": str(exc)}))
        return 2
    except Exception as exc:
        # Driver free-text errors may expose connection strings; print only the class.
        print(json.dumps({"failed": True, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
