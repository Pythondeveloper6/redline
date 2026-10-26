from __future__ import annotations

import pytest

from redline_core.sql import (
    filter_columns,
    fingerprint,
    fingerprint_hash,
    is_explainable,
    primary_table,
    statement_kind,
    tables,
)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("SELECT * FROM t WHERE id = 1", "select *   from t\nwhere id=42"),
        ("SELECT * FROM t WHERE id = %s", "SELECT * FROM t WHERE id = ?"),
        ("SELECT * FROM t WHERE id = :id", "SELECT * FROM t WHERE id = $1"),
        ("SELECT * FROM t WHERE id = %(pk)s", "SELECT * FROM t WHERE id = 7"),
        ("SELECT * FROM t WHERE n = 'a'", "SELECT * FROM t WHERE n = 'it''s'"),
        ("SELECT * FROM t WHERE n = E'x'", "SELECT * FROM t WHERE n = $$y$$"),
        ("SELECT * FROM t WHERE n = $tag$a$tag$", "SELECT * FROM t WHERE n = 'b'"),
        ("SELECT * FROM t WHERE x = 1.5e3", "SELECT * FROM t WHERE x = -2"),
        ("SELECT * FROM t WHERE b = 0xFF", "SELECT * FROM t WHERE b = 1"),
        ("SELECT * FROM t WHERE id IN (1, 2, 3)", "SELECT * FROM t WHERE id IN (%s)"),
        ("SELECT * FROM t WHERE id IN (?, ?)", "select * from t where id in (?,?,?,?,?)"),
        ("INSERT INTO t (a, b) VALUES (1, 2)", "INSERT INTO t (a, b) VALUES (%s, %s), (%s, %s)"),
        ("SELECT 1 -- comment", "SELECT /* hi */ 1"),
        ("SELECT * FROM t WHERE a = ?;", "SELECT * FROM t WHERE a = ?"),
    ],
)
def test_equivalent_fingerprints(a, b):
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint_hash(a) == fingerprint_hash(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("SELECT * FROM t WHERE id = 1", "SELECT * FROM u WHERE id = 1"),
        ('SELECT "Name" FROM t', 'SELECT "name" FROM t'),
        ("SELECT * FROM t WHERE a = 1", "SELECT * FROM t WHERE b = 1"),
        ("SELECT * FROM t WHERE a IS NULL", "SELECT * FROM t WHERE a IS NOT NULL"),
    ],
)
def test_distinct_fingerprints(a, b):
    assert fingerprint(a) != fingerprint(b)


def test_fingerprint_shape():
    fp = fingerprint(
        'select "shop_item"."id", t2 from "shop_item" where "shop_item"."order_id" = 17 '
        "and price - 5 > 0 and name like 'a%' and x in (1,2)"
    )
    assert fp == (
        'SELECT "shop_item"."id", t2 FROM "shop_item" WHERE "shop_item"."order_id" = ? '
        "AND price - ? > ? AND name LIKE ? AND x IN (...)"
    )


def test_literal_contents_never_leak_into_fingerprint():
    fp = fingerprint("SELECT * FROM users WHERE password = 'hunter2' AND note = 'DROP TABLE x;'")
    assert "hunter2" not in fp and "DROP" not in fp
    assert is_explainable("SELECT * FROM users WHERE note = 'DROP TABLE x; DELETE'")


def test_identifiers_and_numbers_in_names_are_kept():
    fp = fingerprint("SELECT col1, t2.x3 FROM table_2 WHERE id = 3")
    assert "col1" in fp and "t2.x3" in fp and "table_2" in fp


@pytest.mark.parametrize(
    ("sql", "ok"),
    [
        ("SELECT 1", True),
        ("  (SELECT 1) UNION (SELECT 2)", True),
        ("WITH a AS (SELECT 1) SELECT * FROM a", True),
        ("select * from t for update", False),
        ("WITH a AS (DELETE FROM t RETURNING *) SELECT * FROM a", False),
        ("WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a", False),
        ("INSERT INTO t VALUES (1)", False),
        ("UPDATE t SET a = 1", False),
        ("DELETE FROM t", False),
        ("SELECT 1; DROP TABLE t", False),
        ("SELECT * INTO new_table FROM t", False),
        ("CREATE TABLE x (a int)", False),
        ("", False),
    ],
)
def test_is_explainable(sql, ok):
    assert is_explainable(sql) is ok


def test_tables_and_columns():
    sql = (
        'SELECT * FROM "shop_order" o INNER JOIN shop_item AS i ON i.order_id = o.id '
        "WHERE o.customer_id = %s AND i.qty > 2 ORDER BY o.created"
    )
    assert tables(sql) == ["shop_order", "shop_item"]
    assert primary_table(sql) == "shop_order"
    cols = filter_columns(sql)
    assert cols["shop_item"] == ["order_id", "qty"]
    assert cols["shop_order"] == ["customer_id"]
    assert statement_kind(sql) == "SELECT"
    assert filter_columns("SELECT * FROM t WHERE f::text = ?") == {"t": ["f"]}
