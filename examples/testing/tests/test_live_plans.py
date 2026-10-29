"""Explicit plan, compilation and catalog checks on guarded disposable databases."""

from __future__ import annotations

import os

import pytest
from live_database_helpers import VENDORS, isolated_engine

pytestmark = pytest.mark.skipif(
    os.environ.get("REDLINE_TEST_COMPOSE") != "1",
    reason="Requires explicit disposable Compose integration environment",
)


def assert_normal_sql(engine):
    """A leaked NOEXEC/SHOWPLAN setting cannot return the expected ordinary row."""
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1


def row_ids(engine, table):
    from sqlalchemy import select

    with engine.connect() as connection:
        return connection.execute(select(table.c.id).order_by(table.c.id)).scalars().all()


@pytest.mark.parametrize("vendor", VENDORS)
def test_live_estimated_plan_compile_and_cleanup(vendor):
    from redline_core.contrib.sqlalchemy import build_executor
    from redline_core.explain import run_explain
    from redline_core.sql_compile import compile_statement, schema_snapshot

    with isolated_engine(vendor) as (engine, table):
        with engine.begin() as connection:
            connection.execute(table.insert(), [{"id": 1}, {"id": 2}])
        expected = [1, 2]
        canonical_vendor = "postgresql" if vendor == "postgres" else vendor
        executor = build_executor(engine, canonical_vendor, lambda: None)
        sql = f"SELECT id FROM {table.name} WHERE id = 1"
        estimated = run_explain(sql, None, canonical_vendor, executor)
        assert estimated.vendor == canonical_vendor and estimated.analyzed is False
        assert estimated.plan and estimated.tree
        assert_normal_sql(engine)
        assert row_ids(engine, table) == expected

        valid = compile_statement(engine, sql)
        assert valid["status"] == "compiled"
        assert_normal_sql(engine)
        # A literal absent ID makes accidental execution directly observable. No outer
        # rollback hides an accidental INSERT: inspect from an independent checkout.
        dml = compile_statement(engine, f"INSERT INTO {table.name} (id) VALUES (31337)")
        assert dml["status"] == "compiled"
        assert_normal_sql(engine)
        assert row_ids(engine, table) == expected

        missing = compile_statement(engine, f"SELECT missing_redline_column FROM {table.name}")
        if vendor == "mssql":
            # NOEXEC supports deferred resolution. A compiled result is not a schema
            # guarantee; an implementation/server that catches this error is also valid.
            assert missing["status"] in {"compiled", "error"}
            if missing["status"] == "compiled":
                assert "deferred" in missing["limitation"]
        else:
            assert missing["status"] == "error"
            assert missing["error"]["category"] == "schema"
        assert_normal_sql(engine)
        assert row_ids(engine, table) == expected

        snapshot = schema_snapshot(engine)
        assert snapshot["vendor"] == canonical_vendor
        assert any(column["name"] == "id" for column in snapshot["tables"][table.name])
        assert_normal_sql(engine)

        if vendor == "mssql":
            from redline_core.database_mssql import inspect_query

            actual = run_explain(sql, None, "mssql", executor, analyze=True, allow_analyze=True)
            assert actual.analyzed is True and actual.plan and actual.tree
            assert_normal_sql(engine)
            assert row_ids(engine, table) == expected
            # The primary-key index is created by the isolated table fixture. Metadata
            # visibility and SHOWPLAN permissions are mandatory in this integration tier.
            catalog = inspect_query(sql, executor)
            assert any(column["name"] == "id" for column in catalog["columns"])
            assert any("id" in index["columns"] for index in catalog["indexes"])
            assert catalog["table"].endswith("." + table.name)
            assert_normal_sql(engine)
