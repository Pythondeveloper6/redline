import sqlite3

from redline_core.database_checks import inspect_query
from redline_core.plan_analysis import analyze_postgres


def test_sqlite_wide_columns_and_covering_candidate():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE events (id INTEGER, kind INTEGER, amount REAL, body TEXT)")
    statements = []

    def executor(sql, params):
        statements.append(sql)
        return connection.execute(sql, params or []).fetchall()

    report = inspect_query("SELECT * FROM events WHERE kind = 1", "sqlite", executor)
    assert report["findings"][0]["kind"] == "wide_columns"
    report = inspect_query("SELECT SUM(amount) FROM events WHERE kind = 1", "sqlite", executor)
    assert any(f["kind"] == "covering_index" for f in report["findings"])
    assert all(s.startswith(("SELECT", "PRAGMA")) for s in statements)
    connection.close()


def test_index_that_covers_aggregate_avoids_duplicate_advice():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE events (kind INTEGER, amount REAL)")
    connection.execute("CREATE INDEX ix_events ON events (kind, amount)")
    report = inspect_query(
        "SELECT SUM(amount) FROM events WHERE kind = 1",
        "sqlite",
        lambda sql, params: connection.execute(sql, params or []).fetchall(),
    )
    assert not any(f["kind"] == "covering_index" for f in report["findings"])
    connection.close()


def test_plan_reports_actual_filtered_rows_without_claiming_better_index():
    plan = [
        {
            "Plan": {
                "Node Type": "Index Scan",
                "Relation Name": "events",
                "Actual Rows": 1,
                "Rows Removed by Filter": 10000,
                "Actual Loops": 1,
            }
        }
    ]
    analysis = analyze_postgres(plan)
    assert any("Most scanned rows" in f.title for f in analysis.findings)
