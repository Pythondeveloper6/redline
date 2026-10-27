"""Workload index advisor (``redline_core.index_advisor``, ``GET api/indexes``)."""

from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from redline_core import sql as sqlmod
from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.index_advisor import (
    Requirement,
    _Query,
    advise,
    covers,
    create_sql,
    load_catalog,
    parse_pg_indexdef,
    query_requirements,
    select_indexes,
)
from redline_core.storage.base import SqlStats

from .conftest import make_trace, sql_span

R = Requirement


def req(sql, params=None, vendor="postgresql"):
    return query_requirements(sql, params, vendor)


# ── static requirements ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sql", "params", "expected"),
    [
        ("SELECT * FROM t WHERE a = ?", None, [R("t", frozenset({"a"}))]),
        ("SELECT * FROM t WHERE a = ? AND b = ?", None, [R("t", frozenset({"a", "b"}))]),
        ("SELECT * FROM t WHERE a > ?", None, [R("t", frozenset(), ("a",))]),
        ("SELECT * FROM t WHERE a BETWEEN ? AND ?", None, [R("t", frozenset(), ("a",))]),
        # equality first, then exactly one range column
        (
            "SELECT * FROM t WHERE c > ? AND a = ? AND d < ?",
            None,
            [R("t", frozenset({"a"}), ("c",))],
        ),
        ("SELECT * FROM t WHERE a IN (?, ?, ?)", None, [R("t", frozenset({"a"}))]),
        ("SELECT * FROM t WHERE a IS NULL", None, [R("t", frozenset({"a"}))]),
        # ORDER BY after equality columns
        (
            "SELECT * FROM t WHERE a = ? ORDER BY b DESC, c DESC",
            None,
            [R("t", frozenset({"a"}), ("b", "c"))],
        ),
        # range on the sort column: (eq, range = order)
        (
            "SELECT * FROM t WHERE a = ? AND b > ? ORDER BY b LIMIT 5",
            None,
            [R("t", frozenset({"a"}), ("b",))],
        ),
        # mixed directions cannot be served by one B-tree scan
        ("SELECT * FROM t WHERE a = ? ORDER BY b ASC, c DESC", None, [R("t", frozenset({"a"}))]),
        # sort only: an index helps a top-N query
        (
            "SELECT * FROM t ORDER BY created DESC LIMIT 10",
            None,
            [R("t", frozenset(), ("created",))],
        ),
        ("SELECT * FROM t ORDER BY created DESC", None, []),
        # the primary key is assumed indexed
        ("SELECT * FROM t WHERE id = ?", None, []),
        # joins: one requirement per side (the id side is skipped)
        (
            "SELECT * FROM o JOIN i ON i.order_id = o.id WHERE o.customer_id = ?",
            None,
            [R("i", frozenset({"order_id"})), R("o", frozenset({"customer_id"}))],
        ),
        (
            'SELECT "a"."x" FROM "a" INNER JOIN "b" ON ("a"."b_id" = "b"."ref")',
            None,
            [R("a", frozenset({"b_id"})), R("b", frozenset({"ref"}))],
        ),
        # B-tree-hostile predicates are skipped
        ("SELECT * FROM t WHERE lower(email) = ?", None, []),
        ("SELECT * FROM t WHERE a != ? AND b NOT IN (?, ?) AND c IS NOT NULL", None, []),
        ("SELECT * FROM t WHERE a NOT BETWEEN ? AND ?", None, []),
        ("SELECT * FROM t WHERE a NOT LIKE ?", ["x%"], []),
        # LIKE: a known prefix is a range; unknown patterns give nothing
        ("SELECT * FROM t WHERE name LIKE ?", ["abc%"], [R("t", frozenset(), ("name",))]),
        ("SELECT * FROM t WHERE name LIKE ?", None, []),
        ("SELECT * FROM t WHERE name LIKE ?", ["[redacted]"], []),
        # leading wildcard / ILIKE → a text-search alternative
        ("SELECT * FROM t WHERE name LIKE ?", ["%abc"], [R("t", tail=("name",), kind="trigram")]),
        ("SELECT * FROM t WHERE name ILIKE ?", ["abc%"], [R("t", tail=("name",), kind="trigram")]),
        # OR across columns: one requirement per branch, sharing the AND-ed column
        (
            "SELECT * FROM t WHERE a = ? AND (b = ? OR c = ?)",
            None,
            [R("t", frozenset({"a", "b"})), R("t", frozenset({"a", "c"}))],
        ),
        (
            "SELECT * FROM t WHERE b = ? OR c = ?",
            None,
            [R("t", frozenset({"b"})), R("t", frozenset({"c"}))],
        ),
        # writes are never analysed
        ("UPDATE t SET a = ? WHERE b = ?", None, []),
        ("SELECT * FROM t WHERE a = ? FOR UPDATE", None, []),
    ],
)
def test_query_requirements(sql, params, expected):
    assert req(sql, params) == expected


def test_text_kind_per_vendor():
    sql, params = "SELECT * FROM t WHERE name LIKE ?", ["%abc%"]
    assert req(sql, params, "sqlite") == [R("t", frozenset(), ("name",), "fts")]
    assert req(sql, params, "mysql") == [R("t", frozenset(), ("name",), "fulltext")]
    assert req(sql, params, "other") == []


def test_aliases_and_django_quoting():
    sql = (
        'SELECT "shop_order"."id" FROM "shop_order" WHERE "shop_order"."customer_id" = %s '
        'ORDER BY "shop_order"."created" DESC'
    )
    assert req(sql, [1]) == [R("shop_order", frozenset({"customer_id"}), ("created",))]
    # normalised form gives the same requirement
    assert req(sqlmod.fingerprint(sql)) == req(sql, [1])


def test_covers():
    r = R("t", frozenset({"a", "b"}), ("c",))
    assert covers(["a", "b", "c"], r)
    assert covers(["B", "A", "c", "d"], r)  # eq in any order, extra trailing columns
    assert not covers(["a", "c", "b"], r)
    assert not covers(["a", "b"], r)
    assert covers(["a", "b"], R("t", frozenset({"a"})))
    assert not covers(["b", "a"], R("t", frozenset({"a"})))


# ── DDL ──────────────────────────────────────────────────────────────────────────────


def test_create_sql_quoting():
    assert create_sql("orders", ["customer_id", "created"], "postgresql") == (
        "CREATE INDEX ix_orders_customer_id_created ON orders (customer_id, created);"
    )
    assert create_sql("Order", ["userId"], "mysql") == (
        "CREATE INDEX ix_order_userid ON `Order` (`userId`);"
    )
    assert create_sql("Order", ["userId"], "postgresql") == (
        'CREATE INDEX ix_order_userid ON "Order" ("userId");'
    )
    assert "USING gin (name gin_trgm_ops)" in create_sql("t", ["name"], "postgresql", "trigram")
    assert create_sql("t", ["name"], "postgresql", "trigram").startswith(
        "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
    )
    assert create_sql("t", ["name"], "sqlite", "fts") == (
        "CREATE VIRTUAL TABLE t_name_fts USING fts5(name, content='t', tokenize='trigram');"
    )
    assert create_sql("t", ["name"], "mysql", "fulltext") == (
        "ALTER TABLE t ADD FULLTEXT INDEX ix_t_name_ft (name);"
    )
    long = create_sql("t" * 40, ["c" * 40], "postgresql")
    name = long.split()[2]
    assert len(name) <= 63


def test_parse_pg_indexdef():
    assert parse_pg_indexdef("CREATE INDEX ix ON public.t USING btree (a, b DESC NULLS LAST)") == (
        ["a", "b"],
        "btree",
    )
    assert parse_pg_indexdef(
        'CREATE UNIQUE INDEX t_pkey ON public.t USING btree ("Id") INCLUDE (x)'
    ) == (["Id"], "btree")
    assert parse_pg_indexdef("CREATE INDEX ix ON t USING btree (lower(email), a)") == ([], "btree")
    assert parse_pg_indexdef("CREATE INDEX ix ON t USING btree (a) WHERE (b IS NULL)") is None
    assert parse_pg_indexdef("CREATE INDEX ix ON t USING gin (name gin_trgm_ops)") == (
        ["name"],
        "trigram",
    )
    assert parse_pg_indexdef("CREATE INDEX ix ON t USING hash (a)") == (["a"], "hash")


# ── set cover ────────────────────────────────────────────────────────────────────────


def q(fp, total, *reqs, vendor="postgresql", scanned=None, count=1):
    return _Query(fp, f"-- {fp}", count, total, vendor, list(reqs), scanned=scanned)


def test_set_cover_merges_prefixes():
    queries = [
        q("f1", 100.0, R("t", frozenset({"a"}))),
        q("f2", 50.0, R("t", frozenset({"a", "b"}))),
        q("f3", 10.0, R("u", frozenset({"x"}))),
    ]
    out = select_indexes(queries, verified={})
    assert [(c["table"], c["columns"]) for c in out] == [("t", ["a", "b"]), ("u", ["x"])]
    first = out[0]
    assert first["covers"] == ["f1", "f2"]
    assert first["total_ms"] == 150.0
    assert first["est_saving_ms"] == 60.0  # 40 % without an EXPLAIN
    assert first["confidence"] == "medium"
    assert first["verified"] is False


def test_set_cover_orders_equality_columns_by_shared_use():
    # (status, tenant) and (tenant): tenant first so one index serves both
    queries = [
        q("f1", 10.0, R("t", frozenset({"status", "tenant"}))),
        q("f2", 30.0, R("t", frozenset({"tenant"}))),
    ]
    out = select_indexes(queries, verified={})
    assert len(out) == 1
    assert out[0]["columns"] == ["tenant", "status"]


def test_set_cover_scan_saving_and_confidence():
    queries = [q("f1", 100.0, R("t", frozenset({"a"})), scanned={"t"})]
    (c,) = select_indexes(queries, verified={"postgresql": True})
    assert c["est_saving_ms"] == 80.0
    assert c["confidence"] == "high"
    assert c["verified"] is True
    queries = [q("f1", 100.0, R("t", frozenset({"a"})), scanned=set())]
    assert select_indexes(queries, verified={})[0]["confidence"] == "low"


def test_set_cover_splits_weight_and_caps():
    # a query with two requirements credits each half
    queries = [q("f1", 100.0, R("t", frozenset({"a"})), R("u", frozenset({"b"})))]
    out = select_indexes(queries, verified={})
    assert [c["est_saving_ms"] for c in out] == [20.0, 20.0]
    many = [q(f"f{i}", float(100 - i), R("t", frozenset({f"c{i}"}))) for i in range(25)]
    out = select_indexes(many, verified={})
    assert len(out) == 10
    assert out[0]["columns"] == ["c0"]


def test_set_cover_text_candidates():
    queries = [q("f1", 40.0, R("t", frozenset(), ("name",), "trigram"))]
    (c,) = select_indexes(queries, verified={})
    assert c["kind"] == "trigram"
    assert c["confidence"] == "low"
    assert "pg_trgm" in c["note"]


# ── end to end on SQLite ─────────────────────────────────────────────────────────────


def sqlite_executor(path):
    def execute(sql, params):
        conn = sqlite3.connect(path)
        try:
            conn.execute("BEGIN")
            return conn.execute(sql, params or ()).fetchall()
        finally:
            conn.rollback()
            conn.close()

    return execute


@pytest.fixture
def app_db(tmp_path):
    path = str(tmp_path / "app.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INT, status TEXT, created REAL);
        CREATE TABLE items (id INTEGER PRIMARY KEY, order_id INT, sku TEXT, name TEXT);
        CREATE INDEX items_sku ON items (sku);
        CREATE INDEX orders_expr ON orders (lower(status));
        """
    )
    conn.executemany(
        "INSERT INTO orders (customer_id, status, created) VALUES (?, ?, ?)",
        [(i % 50, "new" if i % 3 else "paid", float(i)) for i in range(500)],
    )
    conn.commit()
    conn.close()
    return path


WORKLOAD = [
    # (sql, params, duration_ms, repeats)
    ("SELECT * FROM orders WHERE customer_id = ? ORDER BY created DESC", [7], 5.0, 20),
    ("SELECT * FROM orders WHERE customer_id = ?", [7], 2.0, 10),
    ("SELECT * FROM items WHERE sku = ?", ["A1"], 4.0, 10),  # already indexed
    ("SELECT * FROM items WHERE order_id = ?", [3], 1.0, 10),
    ("SELECT * FROM items WHERE name LIKE ?", ["%bolt%"], 3.0, 5),
    ("SELECT * FROM orders WHERE id = ?", [1], 9.0, 10),  # primary key
    ("UPDATE orders SET status = ? WHERE customer_id = ?", ["x", 1], 50.0, 5),  # a write
]


def fill(store, workload=WORKLOAD, alias="default"):
    now = time.time()
    for sql, params, ms, n in workload:
        spans = []
        for _ in range(n):
            s = sql_span(sql, params, duration_ms=ms)
            s.data["db_alias"] = alias
            spans.append(s)
        store.save(make_trace(started_at=now - 5, spans=spans))


def test_advise_sqlite_end_to_end(store, app_db):
    fill(store)
    payload = advise(
        store.sql_workload(3600),
        get_trace=lambda tid: store.get(tid, include_profile=False),
        executor_factory=lambda alias, vendor: sqlite_executor(app_db),
    )
    json.dumps(payload)
    assert payload["verified"] is True
    assert payload["vendor"] == "sqlite"
    assert payload["unused_supported"] is False
    assert payload["unused_error"] is None
    assert payload["already_indexed"] >= 1  # items(sku)
    by_cols = {(c["table"], tuple(c["columns"])): c for c in payload["candidates"]}
    assert set(by_cols) == {
        ("orders", ("customer_id", "created")),
        ("items", ("order_id",)),
        ("items", ("name",)),
    }
    orders = by_cols[("orders", ("customer_id", "created"))]
    fp_sorted = sqlmod.fingerprint_hash(WORKLOAD[0][0])
    fp_plain = sqlmod.fingerprint_hash(WORKLOAD[1][0])
    assert orders["covers"] == [fp_sorted, fp_plain]
    assert orders["queries"] == 30
    assert orders["total_ms"] == 120.0
    assert orders["est_saving_ms"] == 96.0  # SQLite scanned orders: 80 %
    assert orders["confidence"] == "high"
    assert orders["create_sql"] == (
        "CREATE INDEX ix_orders_customer_id_created ON orders (customer_id, created);"
    )
    assert orders["kind"] == "btree" and orders["verified"] is True
    fts = by_cols[("items", ("name",))]
    assert fts["kind"] == "fts"
    assert "fts5" in fts["create_sql"]
    # ranked by covered time
    assert payload["candidates"][0] is orders


def test_advise_sqlite_existing_fts_and_pk(store, tmp_path):
    path = str(tmp_path / "fts.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE docs (code TEXT PRIMARY KEY, body TEXT) WITHOUT ROWID;
        CREATE VIRTUAL TABLE docs_body_fts USING fts5(body, content='docs', tokenize='trigram');
        """
    )
    conn.close()
    fill(
        store,
        [
            ("SELECT * FROM docs WHERE body LIKE ?", ["%x%"], 3.0, 3),
            ("SELECT * FROM docs WHERE code = ?", ["a"], 3.0, 3),
            ("WITH c AS (SELECT 1 AS k) SELECT * FROM c WHERE k = ?", [1], 3.0, 3),
        ],
    )
    payload = advise(
        store.sql_workload(3600),
        get_trace=lambda tid: store.get(tid, include_profile=False),
        executor_factory=lambda alias, vendor: sqlite_executor(path),
    )
    assert payload["candidates"] == []  # fts exists, pk exists, CTE is not a table


def test_load_catalog_sqlite(app_db):
    cat = load_catalog("sqlite", sqlite_executor(app_db))
    assert {"orders", "items"} <= cat.tables
    names = {name: cols for name, cols, _ in cat.indexes["items"]}
    assert names["items_sku"] == ("sku",)
    assert names["(primary key)"] == ("id",)
    assert "orders_expr" not in {n for n, _, _ in cat.indexes["orders"]}  # expression only


def test_advise_static_without_executor(store):
    fill(store)
    payload = advise(store.sql_workload(3600), get_trace=store.get)
    assert payload["verified"] is False
    assert payload["explained"] == 0
    cands = {(c["table"], tuple(c["columns"])) for c in payload["candidates"]}
    assert ("items", ("sku",)) in cands  # cannot know it exists
    assert all(c["verified"] is False for c in payload["candidates"])
    assert all(c["confidence"] in ("medium", "low") for c in payload["candidates"])


def test_advise_filters_db_alias(store):
    fill(store, WORKLOAD[:1], alias="replica")
    assert advise(store.sql_workload(3600), get_trace=store.get)["candidates"] == []
    payload = advise(store.sql_workload(3600), get_trace=store.get, db="replica")
    assert len(payload["candidates"]) == 1


def test_advise_budget_and_broken_executor(store, app_db):
    fill(store)

    def broken(sql, params):
        raise RuntimeError("down")

    payload = advise(
        store.sql_workload(3600),
        get_trace=store.get,
        executor_factory=lambda alias, vendor: broken,
    )
    assert payload["verified"] is False
    assert payload["candidates"]
    payload = advise(
        store.sql_workload(3600),
        get_trace=store.get,
        executor_factory=lambda alias, vendor: sqlite_executor(app_db),
        budget_s=0.0,
    )
    assert payload["verified"] is False and payload["explained"] == 0


def test_executor_only_sees_reads(store, app_db):
    fill(store)
    seen = []
    inner = sqlite_executor(app_db)

    def spy(sql, params):
        seen.append(sql)
        return inner(sql, params)

    advise(store.sql_workload(3600), get_trace=store.get, executor_factory=lambda a, v: spy)
    assert seen
    for stmt in seen:
        assert stmt.startswith(("SELECT ", "EXPLAIN QUERY PLAN SELECT ")), stmt


# ── API ──────────────────────────────────────────────────────────────────────────────


def test_api_indexes_no_executor(store):
    fill(store)
    api = RedlineAPI(store, Settings())
    status, payload = api.dispatch("GET", "api/indexes", {"window": "1h"})
    assert status == 200
    json.dumps(payload)
    assert payload["candidates"]
    assert payload["verified"] is False
    assert payload["unused"] == [] and payload["unused_supported"] is False
    assert payload["window"] == 3600.0 and payload["db"] == "default"
    assert payload["cached"] is False
    c = payload["candidates"][0]
    assert set(c) >= {
        "table", "columns", "create_sql", "covers", "queries", "total_ms", "est_saving_ms",
        "vendor", "kind", "confidence", "verified",
    }  # fmt: skip


def test_api_indexes_pg_without_executor_reports_unused_unavailable(store):
    now = time.time()
    span = sql_span("SELECT * FROM t WHERE a = %s", [1])
    span.data["vendor"] = "postgresql"
    store.save(make_trace(started_at=now - 5, spans=[span]))
    api = RedlineAPI(store, Settings(), explain_executor=lambda alias, vendor: None)
    status, payload = api.dispatch("GET", "api/indexes")
    assert status == 200
    assert payload["unused_supported"] is False
    assert payload["unused_error"]["code"] == "explain_unavailable"
    assert payload["candidates"][0]["create_sql"] == "CREATE INDEX ix_t_a ON t (a);"


def test_api_indexes_cache(store, app_db):
    fill(store)
    calls = []

    def factory(alias, vendor):
        calls.append((alias, vendor))
        return sqlite_executor(app_db)

    api = RedlineAPI(store, Settings(), explain_executor=factory)
    first = api.dispatch("GET", "api/indexes", {"window": "1h"})[1]
    assert first["cached"] is False and first["verified"] is True
    n = len(calls)
    second = api.dispatch("GET", "api/indexes", {"window": "1h"})[1]
    assert second["cached"] is True
    assert second["candidates"] == first["candidates"]
    assert len(calls) == n  # no DB work
    # another window / db is a different key; refresh=1 bypasses
    assert api.dispatch("GET", "api/indexes", {"window": "2h"})[1]["cached"] is False
    assert api.dispatch("GET", "api/indexes", {"window": "1h", "db": "x"})[1]["cached"] is False
    fresh = api.dispatch("GET", "api/indexes", {"window": "1h", "refresh": "1"})[1]
    assert fresh["cached"] is False
    # expiry
    for key, (_, payload) in list(api._index_cache.items()):
        api._index_cache[key] = (0.0, payload)
    assert api.dispatch("GET", "api/indexes", {"window": "1h"})[1]["cached"] is False


def test_api_indexes_errors(store):
    api = RedlineAPI(store, Settings())
    status, payload = api.dispatch("GET", "api/indexes", {"window": "forever"})
    assert (status, payload["error"]["code"]) == (400, "invalid_window")
    status, payload = api.dispatch("POST", "api/indexes")
    assert (status, payload["error"]["code"]) == (405, "method_not_allowed")

    class Legacy:
        def get(self, *a, **k):
            return None

    status, payload = RedlineAPI(Legacy()).dispatch("GET", "api/indexes")
    assert (status, payload["error"]["code"]) == (501, "not_supported")


def test_advise_accepts_plain_sqlstats():
    stats = SqlStats(
        fingerprint="f" * 16,
        sql="SELECT * FROM t WHERE a = ? AND b > ?",
        count=3,
        traces=1,
        total_ms=30.0,
        max_ms=10.0,
        routes=[],
        sample_trace_id="",
        vendor="mysql",
    )
    payload = advise([stats])
    (c,) = payload["candidates"]
    assert c["columns"] == ["a", "b"] and c["vendor"] == "mysql"
    assert payload["unused_supported"] is False and payload["unused_error"] is None


# ── live PostgreSQL (skips cleanly without a server) ─────────────────────────────────

PG_DSN = os.environ.get("REDLINE_TEST_POSTGRES_DSN", "postgresql://localhost/postgres")


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed; Postgres test skipped")
    try:
        conn = psycopg.connect(PG_DSN, connect_timeout=2)
    except Exception as exc:
        pytest.skip(
            f"No PostgreSQL server at {PG_DSN} ({type(exc).__name__}); "
            "set REDLINE_TEST_POSTGRES_DSN"
        )
    schema = f"redline_ix_{os.getpid()}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}")
        cur.execute(
            "CREATE TABLE orders AS SELECT g AS id, g % 100 AS customer_id, "
            "g::float AS created FROM generate_series(1, 20000) g"
        )
        cur.execute("ALTER TABLE orders ADD PRIMARY KEY (id)")
        cur.execute("CREATE INDEX orders_created ON orders (created)")
        cur.execute("CREATE INDEX orders_never ON orders (customer_id, created)")
        cur.execute(
            "CREATE TABLE items AS SELECT g AS id, g % 500 AS order_id "
            "FROM generate_series(1, 200000) g"
        )
        cur.execute("ANALYZE orders")
        cur.execute("ANALYZE items")
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        conn.commit()
        conn.close()


def pg_executor(conn):
    def execute(sql, params):
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT redline")
            try:
                cur.execute(sql, params)
                return cur.fetchall()
            finally:
                cur.execute("ROLLBACK TO SAVEPOINT redline")

    return execute


def test_live_postgres_catalog_and_unused(pg, store):
    ex = pg_executor(pg)
    cat = load_catalog("postgresql", ex)
    assert {"orders", "items"} <= cat.tables
    assert ("created",) in {cols for _, cols, _ in cat.indexes["orders"]}
    now = time.time()
    spans = []
    for sql, params in (
        ("SELECT * FROM orders WHERE created > %s", [5.0]),  # already indexed
        ("SELECT * FROM items WHERE order_id = %s", [3]),
    ):
        s = sql_span(sql, params, duration_ms=5.0)
        s.data["vendor"] = "postgresql"
        spans.append(s)
    store.save(make_trace(started_at=now - 5, spans=spans))
    api = RedlineAPI(store, Settings(), explain_executor=lambda alias, vendor: ex)
    status, payload = api.dispatch("GET", "api/indexes")
    assert status == 200, payload
    assert payload["verified"] is True and payload["unused_supported"] is True
    assert [(c["table"], c["columns"]) for c in payload["candidates"]] == [("items", ["order_id"])]
    assert payload["candidates"][0]["confidence"] == "high"  # Seq Scan on items
    unused = {u["index"]: u for u in payload["unused"]}
    assert "orders_never" in unused
    assert "orders_pkey" not in unused
    assert unused["orders_never"]["size_bytes"] > 0
