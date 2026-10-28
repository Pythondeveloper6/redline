from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from redline_core.explain import ExplainError, explain_statement, run_explain
from redline_core.plan_analysis import (
    analyze_mysql,
    analyze_plan,
    analyze_postgres,
    analyze_sqlite,
    create_index_sql,
    render_text,
)

FIXTURES = Path(__file__).parent / "fixtures"


def flags(tree):
    return {f for n in tree.walk() for f in n.flags}


# ── PostgreSQL ────────────────────────────────────────────────────────────────


def test_postgres_fixture():
    plan = json.loads((FIXTURES / "pg_seq_scan.json").read_text())
    a = analyze_postgres(plan, "SELECT ... ")
    assert a.tree.label == "Sort"
    assert a.tree.children[0].label == "Hash Join"
    scan = a.tree.children[0].children[0]
    assert scan.label == "Seq Scan on shop_orderitem i"
    assert scan.rows == 100000 and scan.cost == 1693.0 and scan.actual_ms == 9.1
    assert flags(a.tree) == {"seq_scan", "sort_spill", "rows_filtered_out"}
    titles = [f.title for f in a.findings]
    assert "Sort spilled to disk" in titles
    assert "Sequential scan on shop_order" in titles
    assert (
        "CREATE INDEX shop_order_customer_id_status_idx ON shop_order (customer_id, status);"
        in a.suggested_indexes
    )
    # the join column of the large seq-scanned child gets an index too
    assert (
        "CREATE INDEX shop_orderitem_order_id_idx ON shop_orderitem (order_id);"
        in a.suggested_indexes
    )
    assert "Execution Time: 21.6 ms" in a.tree.detail
    text = render_text(a.tree)
    assert "Seq Scan on shop_orderitem" in text and "sort_spill" in text


def test_postgres_accepts_string_and_nested_list():
    plan = (FIXTURES / "pg_seq_scan.json").read_text()
    assert analyze_postgres(plan).tree.label == "Sort"
    assert analyze_postgres([json.loads(plan)]).tree.label == "Sort"


def test_postgres_small_seq_scan_and_nested_loop():
    plan = [
        {
            "Plan": {
                "Node Type": "Nested Loop",
                "Join Type": "Inner",
                "Plan Rows": 50000,
                "Total Cost": 9000.0,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "tiny",
                        "Alias": "tiny",
                        "Plan Rows": 10,
                        "Total Cost": 1.1,
                    },
                    {
                        "Node Type": "Index Scan",
                        "Relation Name": "big",
                        "Alias": "big",
                        "Index Name": "big_pkey",
                        "Index Cond": "(id = tiny.big_id)",
                        "Plan Rows": 1,
                        "Total Cost": 0.3,
                    },
                ],
            }
        }
    ]
    a = analyze_postgres(plan)
    assert flags(a.tree) == {"nested_loop_high_rows"}
    assert a.tree.children[1].detail.startswith("Index Name: big_pkey")
    assert a.suggested_indexes == []


def test_postgres_unfiltered_large_scan_is_info():
    plan = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "events",
                "Plan Rows": 50000,
                "Total Cost": 900,
            }
        }
    ]
    a = analyze_postgres(plan, "SELECT * FROM events")
    assert [f.severity for f in a.findings] == ["info"] and a.suggested_indexes == []


# ── MySQL ─────────────────────────────────────────────────────────────────────


def test_mysql_fixture():
    a = analyze_mysql((FIXTURES / "mysql_full_scan.json").read_text())
    assert a.tree.label == "Query block #1" and a.tree.cost == 10245.2
    sort = a.tree.children[0]
    assert sort.label == "Sort" and "temp_sort" in sort.flags
    loop = sort.children[0]
    assert loop.label == "Nested loop"
    first, second = loop.children
    assert first.label == "Full table scan on shop_order" and first.rows == 50000
    assert "full_scan" in first.flags and "full_scan" in second.flags
    assert a.suggested_indexes == [
        "CREATE INDEX shop_order_customer_id_idx ON shop_order (customer_id);",
        "CREATE INDEX shop_orderitem_order_id_idx ON shop_orderitem (order_id);",
    ]
    assert all(f.severity == "warning" for f in a.findings if f.title.startswith("Full"))


def test_mysql_index_lookup_is_clean():
    plan = {
        "query_block": {
            "select_id": 1,
            "table": {
                "table_name": "t",
                "access_type": "ref",
                "key": "t_a",
                "rows_examined_per_scan": 3,
            },
        }
    }
    a = analyze_mysql(plan)
    assert a.findings == [] and a.tree.children[0].label == "Index lookup on t"


# ── SQLite (real EXPLAIN QUERY PLAN through stdlib sqlite3) ────────────────────


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE shop_order (id INTEGER PRIMARY KEY, customer_id INT, status TEXT);
        CREATE TABLE shop_orderitem (id INTEGER PRIMARY KEY, order_id INT, qty INT);
        CREATE INDEX item_qty ON shop_orderitem (qty);
        """
    )
    yield conn
    conn.close()


def executor_for(conn, log=None):
    def execute(sql, params):
        if log is not None:
            log.append(sql)
        conn.execute("SAVEPOINT redline_explain")
        try:
            return conn.execute(sql, params or ()).fetchall()
        finally:
            conn.execute("ROLLBACK TO redline_explain")
            conn.execute("RELEASE redline_explain")

    return execute


def test_sqlite_missing_index_is_flagged(db):
    sql = "SELECT * FROM shop_order WHERE customer_id = ?"
    r = run_explain(sql, [1], "sqlite", executor_for(db))
    assert r.vendor == "sqlite" and r.analyzed is False
    assert r.tree.children[0].label == "SCAN shop_order"
    assert r.tree.children[0].flags == ["full_scan"]
    assert r.suggested_indexes == [
        "CREATE INDEX shop_order_customer_id_idx ON shop_order (customer_id);"
    ]
    assert r.findings[0].severity == "warning"
    assert isinstance(r.plan, list) and "SCAN shop_order" in r.text
    # applying the suggestion fixes the plan
    db.execute(r.suggested_indexes[0])
    r2 = run_explain(sql, [1], "sqlite", executor_for(db))
    assert r2.findings == [] and r2.tree.children[0].label.startswith(
        "SEARCH shop_order USING INDEX"
    )


def test_sqlite_join_alias_and_temp_btree(db):
    sql = (
        "SELECT * FROM shop_order AS o JOIN shop_orderitem i ON i.order_id = o.id "
        "WHERE o.status = ? ORDER BY o.status, i.qty"
    )
    r = run_explain(sql, ["paid"], "sqlite", executor_for(db))
    assert r.tree.children[0].label == "SCAN i"  # SQLite reports the alias
    assert r.suggested_indexes == [
        "CREATE INDEX shop_orderitem_order_id_idx ON shop_orderitem (order_id);"
    ]
    assert "temp_sort" in flags(r.tree)
    assert all(f.title.startswith(("Full table scan", "Sort", "Automatic")) for f in r.findings)


def test_sqlite_unfiltered_scan_is_info_and_index_scan_clean(db):
    r = run_explain("SELECT count(*) FROM shop_order", None, "sqlite", executor_for(db))
    assert [f.severity for f in r.findings] == ["info"] and r.suggested_indexes == []
    r = run_explain("SELECT qty FROM shop_orderitem WHERE qty = ?", [2], "sqlite", executor_for(db))
    assert r.findings == []
    r = run_explain("SELECT 1", None, "sqlite", executor_for(db))
    assert r.findings == []


def test_sqlite_subquery_tree(db):
    rows = db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM shop_order "
        "WHERE id IN (SELECT order_id FROM shop_orderitem)"
    ).fetchall()
    a = analyze_sqlite(
        rows, "SELECT * FROM shop_order WHERE id IN (SELECT order_id FROM shop_orderitem)"
    )
    labels = [n.label for n in a.tree.walk()]
    assert "LIST SUBQUERY 1" in labels
    sub = next(n for n in a.tree.walk() if n.label == "LIST SUBQUERY 1")
    assert sub.children and sub.children[0].label == "SCAN shop_orderitem"


def test_sqlite_automatic_index_detail():
    a = analyze_sqlite(
        [(3, 0, 0, "SEARCH t USING AUTOMATIC COVERING INDEX (k=?)")], "SELECT * FROM t WHERE k = ?"
    )
    assert a.tree.children[0].flags == ["automatic_index"]
    assert a.suggested_indexes == ["CREATE INDEX t_k_idx ON t (k);"]
    dict_rows = analyze_sqlite([{"id": 2, "parent": 0, "notused": 0, "detail": "SCAN TABLE t"}])
    assert dict_rows.tree.children[0].label == "SCAN TABLE t"


# ── runner safety ─────────────────────────────────────────────────────────────


def test_refuses_writes_and_analyze(db):
    log = []
    ex = executor_for(db, log)
    with pytest.raises(ExplainError) as e:
        run_explain("DELETE FROM shop_order", None, "sqlite", ex)
    assert e.value.code == "not_explainable"
    with pytest.raises(ExplainError) as e:
        run_explain("SELECT 1", None, "postgresql", ex, analyze=True)
    assert e.value.code == "analyze_disabled" and e.value.status == 403
    with pytest.raises(ExplainError) as e:
        run_explain("SELECT 1", None, "oracle", ex)
    assert e.value.code == "unsupported_vendor"
    assert log == []


def test_executor_failure_and_bad_plans():
    def broken(sql, params):
        raise RuntimeError("no such table: x")

    with pytest.raises(ExplainError) as e:
        run_explain("SELECT * FROM x", None, "sqlite", broken)
    assert (
        e.value.code == "explain_failed"
        and e.value.status == 422
        and "no such table" in e.value.message
    )
    with pytest.raises(ExplainError):
        run_explain("SELECT 1", None, "postgresql", lambda s, p: [])
    with pytest.raises(ExplainError):
        run_explain("SELECT 1", None, "mysql", lambda s, p: [("not json",)])


def test_statements_and_fake_postgres_executor():
    assert explain_statement("SELECT 1;", "postgresql") == "EXPLAIN (FORMAT JSON) SELECT 1"
    assert explain_statement("SELECT 1", "postgresql", analyze=True) == (
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT 1"
    )
    assert explain_statement("SELECT 1", "mysql") == "EXPLAIN FORMAT=JSON SELECT 1"
    assert explain_statement("SELECT 1", "sqlite") == "EXPLAIN QUERY PLAN SELECT 1"
    with pytest.raises(ExplainError):
        explain_statement("SELECT 1", "mysql", analyze=True)
    plan = (FIXTURES / "pg_seq_scan.json").read_text()
    seen = []

    def fake(sql, params):
        seen.append((sql, params))
        return [(plan,)]

    r = run_explain(
        "SELECT * FROM shop_order o WHERE customer_id = %s",
        [42],
        "postgresql",
        fake,
        analyze=True,
        allow_analyze=True,
    )
    assert r.analyzed is True and seen[0][0].startswith("EXPLAIN (ANALYZE") and seen[0][1] == [42]
    assert r.to_dict()["tree"]["label"] == "Sort"
    assert set(r.to_dict()) == {
        "vendor",
        "analyzed",
        "plan",
        "text",
        "tree",
        "findings",
        "suggested_indexes",
    }


def test_create_index_sql_quoting_and_dispatch():
    assert create_index_sql("Order", ["Customer Id"]) == (
        'CREATE INDEX "Order_Customer_Id_idx" ON "Order" ("Customer Id");'
    )
    assert create_index_sql("t", ["a b"], "mysql") == "CREATE INDEX t_a_b_idx ON t (`a b`);"
    with pytest.raises(ValueError):
        analyze_plan("oracle", None)


# ── live PostgreSQL (skips cleanly without a server) ──────────────────────────

PG_DSN = os.environ.get("REDLINE_TEST_POSTGRES_DSN", "postgresql://localhost/postgres")


@pytest.fixture
def pg():
    psycopg = pytest.importorskip(
        "psycopg", reason="psycopg not installed; Postgres EXPLAIN test skipped"
    )
    try:
        conn = psycopg.connect(PG_DSN, connect_timeout=2)
    except Exception as exc:
        pytest.skip(
            f"No PostgreSQL server at {PG_DSN} ({type(exc).__name__}); "
            "set REDLINE_TEST_POSTGRES_DSN"
        )
    yield conn
    conn.close()


def test_live_postgres_explain(pg):
    with pg.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE redline_t AS "
            "SELECT g AS id, g % 100 AS k FROM generate_series(1, 500000) g"
        )
        cur.execute("ANALYZE redline_t")

    def executor(sql, params):
        with pg.cursor() as cur:
            cur.execute("SAVEPOINT redline")
            try:
                cur.execute(sql, params)
                return cur.fetchall()
            finally:
                cur.execute("ROLLBACK TO SAVEPOINT redline")

    r = run_explain("SELECT * FROM redline_t WHERE k = %s", [5], "postgresql", executor)
    assert "seq_scan" in flags(r.tree)
    assert r.suggested_indexes == ["CREATE INDEX redline_t_k_idx ON redline_t (k);"]


# ── index advice that must not be wrong ───────────────────────────────────────


def _sqlite_advice(sql, params):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, message TEXT, actor TEXT)")
    return run_explain(sql, params, "sqlite", lambda s, p: conn.execute(s, p).fetchall())


def _pg_seq(table, filt):
    return [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": table,
                "Alias": table,
                "Total Cost": 5000.0,
                "Plan Rows": 200000,
                "Filter": filt,
            }
        }
    ]


LIKE_SQL = "SELECT count(*) FROM audit_log WHERE audit_log.message LIKE ?"


def test_sqlite_leading_wildcard_like_no_btree_index():
    r = _sqlite_advice(LIKE_SQL, ["%refund%"])
    assert r.suggested_indexes == []
    f = next(f for f in r.findings if "can serve LIKE" in f.title)
    assert "fts5" in f.detail.lower() and "not a drop-in" in f.detail


def test_postgres_leading_wildcard_like_suggests_trigram_alternative():
    plan = _pg_seq("audit_log", "((message)::text ~~ '%refund%'::text)")
    a = analyze_postgres(plan, LIKE_SQL.replace("?", "%s"), ["%refund%"])
    assert a.suggested_indexes == []
    f = next(f for f in a.findings if "can serve LIKE" in f.title)
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in f.detail
    assert "USING gin (message gin_trgm_ops)" in f.detail


def test_mysql_leading_wildcard_like_suggests_fulltext():
    plan = {
        "query_block": {
            "select_id": 1,
            "table": {
                "table_name": "audit_log",
                "access_type": "ALL",
                "rows_examined_per_scan": 50000,
                "attached_condition": "(`db`.`audit_log`.`message` like '%refund%')",
            },
        }
    }
    a = analyze_mysql(plan, "SELECT * FROM audit_log WHERE message LIKE '%refund%'")
    assert a.suggested_indexes == []
    f = next(f for f in a.findings if "can serve LIKE" in f.title)
    assert "FULLTEXT" in f.detail and "MATCH(message) AGAINST" in f.detail


def test_pattern_from_literal_without_params():
    plan = _pg_seq("audit_log", "((message)::text ~~ '%x'::text)")
    a = analyze_postgres(plan, "SELECT * FROM audit_log WHERE message LIKE '%x'")
    assert a.suggested_indexes == []


def test_prefix_like_still_gets_btree_with_collation_note():
    plan = _pg_seq("audit_log", "((message)::text ~~ 'abc%'::text)")
    a = analyze_postgres(plan, LIKE_SQL.replace("?", "%s"), ["abc%"])
    assert a.suggested_indexes == ["CREATE INDEX audit_log_message_idx ON audit_log (message);"]
    note = next(f for f in a.findings if "text_pattern_ops" in f.title)
    assert "C collation" in note.detail


def test_unknown_pattern_keeps_old_behaviour():
    plan = _pg_seq("audit_log", "((message)::text ~~ 'x'::text)")
    a = analyze_postgres(plan, LIKE_SQL.replace("?", "%s"))
    assert a.suggested_indexes == ["CREATE INDEX audit_log_message_idx ON audit_log (message);"]


def test_mixed_predicates_keep_the_usable_column():
    sql = "SELECT * FROM audit_log WHERE actor = %s AND message LIKE %s"
    plan = _pg_seq("audit_log", "((actor = 'a') AND ((message)::text ~~ '%x'::text))")
    a = analyze_postgres(plan, sql, ["a", "%x"])
    assert a.suggested_indexes == ["CREATE INDEX audit_log_actor_idx ON audit_log (actor);"]
    assert any("can serve LIKE" in f.title for f in a.findings)


def test_ilike_and_function_wrapped_column():
    plan = _pg_seq("audit_log", "(lower((actor)::text) = 'bob'::text)")
    a = analyze_postgres(plan, "SELECT * FROM audit_log WHERE lower(actor) = %s", ["bob"])
    assert a.suggested_indexes == []
    f = next(f for f in a.findings if "cannot use a plain index" in f.title)
    assert "audit_log (lower(actor))" in f.detail
    plan = _pg_seq("audit_log", "((actor)::text ~~* 'bob%'::text)")
    a = analyze_postgres(plan, "SELECT * FROM audit_log WHERE actor ILIKE %s", ["bob%"])
    assert a.suggested_indexes == []
    assert any("ILIKE" in f.title for f in a.findings)


def test_django_upper_wrapper_like():
    sql = 'SELECT 1 FROM "audit_log" WHERE UPPER("audit_log"."message"::text) LIKE UPPER(%s)'
    plan = _pg_seq("audit_log", "(upper((message)::text) ~~ '%REFUND%'::text)")
    a = analyze_postgres(plan, sql, ["%refund%"])
    assert a.suggested_indexes == []
    assert any("plain index" in f.title for f in a.findings)


def test_not_equal_and_not_in_get_no_index():
    for where, filt in (
        ("level != %s", "((level)::text <> 'info'::text)"),
        ("level NOT IN (%s, %s)", "((level)::text <> ALL ('{a,b}'::text[]))"),
        ("level IS NOT NULL", "(level IS NOT NULL)"),
    ):
        a = analyze_postgres(
            _pg_seq("audit_log", filt), f"SELECT * FROM audit_log WHERE {where}", ["info", "x"]
        )
        assert a.suggested_indexes == [], where
        assert any(f.severity == "info" and "audit_log.level" in f.title for f in a.findings)


def test_equality_unchanged_and_is_null_still_indexed():
    plan = _pg_seq("audit_log", "((actor)::text = 'a'::text)")
    a = analyze_postgres(plan, "SELECT * FROM audit_log WHERE actor = %s", ["a"])
    assert a.suggested_indexes == ["CREATE INDEX audit_log_actor_idx ON audit_log (actor);"]


def test_or_across_columns_gets_separate_indexes():
    sql = "SELECT * FROM audit_log WHERE (actor = %s OR level = %s)"
    plan = _pg_seq("audit_log", "((actor = 'a') OR (level = 'b'))")
    a = analyze_postgres(plan, sql, ["a", "b"])
    assert a.suggested_indexes == [
        "CREATE INDEX audit_log_actor_idx ON audit_log (actor);",
        "CREATE INDEX audit_log_level_idx ON audit_log (level);",
    ]
    sql = "SELECT * FROM audit_log WHERE actor = %s AND level = %s"
    a = analyze_postgres(_pg_seq("audit_log", "((actor = 'a') AND (level = 'b'))"), sql, ["a", "b"])
    assert a.suggested_indexes == [
        "CREATE INDEX audit_log_actor_level_idx ON audit_log (actor, level);"
    ]


def test_run_explain_passes_params_to_analysis():
    plan = json.dumps(_pg_seq("audit_log", "((message)::text ~~ '%refund%'::text)"))
    r = run_explain(LIKE_SQL.replace("?", "%s"), ["%refund%"], "postgresql", lambda s, p: [(plan,)])
    assert r.suggested_indexes == [] and r.findings
