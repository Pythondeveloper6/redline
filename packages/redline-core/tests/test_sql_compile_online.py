from __future__ import annotations

from sqlalchemy import create_engine

from redline_core.check_sql import extract
from redline_core.sql_compile import (
    _mssql_compile_batch,
    _translate_named_binds,
    compare_schema,
    describe_mssql_parameters,
    describe_mssql_result,
    missing_result_columns,
)

SOURCE = """
from sqlalchemy import text


def read(conn, absent):
    row = conn.execute(text("SELECT id, name FROM things WHERE id = :id"), {"id": 1}).fetchone()
    other = {"missing": 1}
    return row["id"], row["missing"], row.name, other["missing"]
"""


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.seen = []

    def exec_driver_sql(self, sql):
        self.seen.append(sql)
        return _Rows(self.rows)


def test_extract_records_literal_row_reads_only():
    statements, skipped = extract(SOURCE, "view.py")
    assert not skipped
    reads = {item["name"] for item in statements[0]["result_reads"]}
    assert {"id", "missing", "name"} <= reads
    # A plain dict in the same scope is not a row object.
    assert "missing" in reads
    assert statements[0]["bindings"] == ["id"]


def test_named_binds_translate_without_touching_literals_or_casts():
    sql = "SELECT :id, 'a:b', col::text FROM t WHERE x = :name -- :comment"
    assert _translate_named_binds(sql) == (
        "SELECT @id, 'a:b', col::text FROM t WHERE x = @name -- :comment"
    )


def test_mssql_batch_declares_typed_parameters_and_skips_existing():
    sql = "DECLARE @declared int;\nSELECT :id, :name, :declared FROM t"
    parameters = [
        {"name": "id", "type": "int"},
        {"name": "name", "type": "nvarchar(50)"},
        {"name": "declared", "type": "int"},
        {"name": "bad-name", "type": "int"},
    ]
    batch = _mssql_compile_batch(sql, parameters)
    assert "DECLARE @id int;" in batch
    assert "DECLARE @name nvarchar(50);" in batch
    assert batch.count("DECLARE @declared") == 1
    assert "@id" in batch and ":id" not in batch


def test_describe_parameters_uses_server_types():
    conn = _Conn([{"name": "id", "suggested_system_type_name": "int"}])
    parameters, limitation = describe_mssql_parameters(conn, "SELECT :id")
    assert limitation is None
    assert parameters == [{"name": "id", "type": "int"}]
    assert "sp_describe_undeclared_parameters" in conn.seen[0]


def test_describe_result_skips_hidden_and_reports_unavailable():
    conn = _Conn(
        [
            {"name": "id", "system_type_name": "int", "is_hidden": False},
            {"name": "secret", "system_type_name": "int", "is_hidden": True},
        ]
    )
    columns, limitation = describe_mssql_result(conn, "SELECT id", ["SELECT"])
    assert limitation is None
    assert columns == [{"name": "id", "type": "int"}]
    conn = _Conn([{"error_number": 11514, "error_message": "no"}])
    columns, limitation = describe_mssql_result(
        conn, "WITH x AS (SELECT 1) SELECT * FROM x", ["WITH"]
    )
    assert columns == [] and limitation


def test_missing_result_columns_is_case_insensitive():
    reads = [{"name": "ID"}, {"name": "absent"}, {"name": "absent"}]
    assert missing_result_columns(reads, [{"name": "id"}]) == ["absent"]


def test_compare_schema_includes_trigger_and_routine_drift():
    before = {
        "tables": {"t": [{"name": "id", "type": "INTEGER", "nullable": True}]},
        "triggers": {"t": [{"name": "tr1", "disabled": False, "instead_of": False}]},
        "routines": {"p": [{"name": "@x", "type": "int"}]},
    }
    after = {
        "tables": {"t": [{"name": "id", "type": "INTEGER", "nullable": True}]},
        "triggers": {"t": [{"name": "tr1", "disabled": True, "instead_of": False}]},
        "routines": {"p": [{"name": "@x", "type": "bigint"}]},
    }
    drift = compare_schema(before, after)
    assert drift["changed_triggers"] == ["t"]
    assert drift["changed_routines"] == ["p"]
    assert drift["affected_tables"] == ["t"]


def test_selective_compilation_skips_unrelated_statements(tmp_path):
    import redline_core.sql_compile as sql_compile

    database = tmp_path / "schema.db"
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE a (id INTEGER)")
            conn.exec_driver_sql("CREATE TABLE b (id INTEGER)")
    finally:
        engine.dispose()
    source = tmp_path / "queries.py"
    source.write_text(
        """
from sqlalchemy import text


def read_a(conn):
    return conn.execute(text("SELECT id FROM a")).fetchall()


def read_b(conn):
    return conn.execute(text("SELECT id FROM b")).fetchall()
"""
    )
    snapshot = tmp_path / "schema.json"
    dsn = f"sqlite:///{database}"
    first = sql_compile.compile_paths([str(source)], dsn, snapshot=str(snapshot))
    assert first[0]["selective"]["compiled"] == 2
    unchanged = sql_compile.compile_paths([str(source)], dsn, snapshot=str(snapshot))
    assert unchanged[0]["selective"]["compiled"] == 0
    assert unchanged[0]["selective"]["skipped_unchanged"] == 2

    engine = create_engine(dsn)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE a ADD COLUMN name TEXT")
    finally:
        engine.dispose()
    changed = sql_compile.compile_paths([str(source)], dsn, snapshot=str(snapshot))
    assert changed[0]["selective"]["compiled"] == 1
    assert changed[0]["selective"]["skipped_unchanged"] == 1
    assert changed[0]["schema_drift"]["changed_tables"] == ["a"]


def test_compile_paths_attaches_online_findings(tmp_path, monkeypatch):
    import redline_core.sql_compile as sql_compile

    source = tmp_path / "view.py"
    source.write_text(SOURCE)

    def fake_compile(engine, sql, *, result_reads=None):
        return {
            "status": "compiled",
            "result_reads": result_reads,
            "result_columns": [{"name": "id"}],
            "result_reads_missing": ["missing"],
        }

    monkeypatch.setattr(sql_compile, "compile_statement", fake_compile)
    engine = create_engine("sqlite://")
    try:
        reports = sql_compile.compile_paths([str(source)], "sqlite://")
    finally:
        engine.dispose()
    compiled = [item for item in reports if item["status"] == "compiled"]
    assert compiled and compiled[0]["result_columns"] == [{"name": "id"}]
    finding = compiled[0]["findings"][0]
    assert finding["kind"] == "result-column-not-returned"
    assert "missing" in finding["detail"]
    assert len(finding["id"]) == 20
