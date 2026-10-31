from __future__ import annotations

import json

import pytest

from redline_core.check_sql import extract, scan
from redline_core.cli import main


def _sql(source):
    statements, skipped = extract(source)
    return [s["sql"] for s in statements], skipped


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('T = "users"\nconn.execute("SELECT * FROM " + T)\n', "SELECT * FROM users"),
        (
            'T = "users"\nconn.execute("SELECT * FROM %s WHERE id = 1" % T)\n',
            "SELECT * FROM users WHERE id = 1",
        ),
        ('conn.execute("SELECT %s, %s" % ("a", "b"))\n', "SELECT a, b"),
        ('T = "users"\nconn.execute("SELECT * FROM {}".format(T))\n', "SELECT * FROM users"),
        (
            'conn.execute("SELECT * FROM {t} LIMIT {n}".format(t="users", n=5))\n',
            "SELECT * FROM users LIMIT 5",
        ),
        (
            'conn.execute(" ".join(["SELECT *", "FROM users", "WHERE id = 1"]))\n',
            "SELECT * FROM users WHERE id = 1",
        ),
        (
            'PARTS = ("SELECT *", "FROM users")\nconn.execute(" ".join(PARTS))\n',
            "SELECT * FROM users",
        ),
        (
            'W = "WHERE id = 1"\nconn.execute(f"SELECT * FROM users {W}")\n',
            "SELECT * FROM users WHERE id = 1",
        ),
        ('Q = "SELECT *"\nQ += " FROM users"\nconn.execute(Q)\n', "SELECT * FROM users"),
    ],
)
def test_constant_composition_forms_resolve(source, expected):
    sql, skipped = _sql(source)
    assert sql == [expected]
    assert not skipped


def test_local_reassignments_are_followed_in_straight_line_code():
    source = """
BASE = "SELECT * FROM users"

def run(conn):
    sql = BASE
    sql = sql + " WHERE active = 1"
    sql += " ORDER BY id"
    conn.execute(sql)
"""
    sql, skipped = _sql(source)
    assert sql == ["SELECT * FROM users WHERE active = 1 ORDER BY id"]
    assert not skipped


def test_conditional_local_reassignment_stays_unresolved():
    source = """
def run(conn, flag):
    sql = "SELECT * FROM users"
    if flag:
        sql = sql + " WHERE active = 1"
    conn.execute(sql)
"""
    sql, skipped = _sql(source)
    assert not sql
    assert skipped[0]["category"] == "unknown_name"


def test_parameters_and_loop_assignments_are_not_treated_as_constants():
    source = """
SQL = "SELECT 1"

def run(conn, SQL):
    conn.execute(SQL)

def loop(conn):
    q = "SELECT 1"
    for part in ("a", "b"):
        q = q + part
        conn.execute(q)
"""
    sql, skipped = _sql(source)
    assert not sql
    assert len(skipped) == 2


def test_unsafe_format_fields_and_runtime_values_are_unresolved():
    source = """
conn.execute("SELECT {0.__class__}".format("x"))
conn.execute("SELECT %s" % user_input)
conn.execute("SELECT {}".format(compute()))
conn.execute("SELECT %s %s" % ("only one",))
"""
    sql, skipped = _sql(source)
    assert not sql
    assert [s["category"] for s in skipped] == [
        "unsafe_format",
        "unknown_name",
        "call",
        "unsafe_format",
    ]


def test_unresolved_counts_by_reason_in_scan_and_json_cli(tmp_path, capsys):
    (tmp_path / "m.py").write_text(
        "from sqlalchemy import select\n"
        'conn.execute("SELECT %s" % user_input)\n'
        "conn.execute(build())\n"
        "conn.execute(select(User))\n"
        'conn.execute("SELECT 1 FROM " + "t")\n'
    )
    report = scan([str(tmp_path)])
    assert report["unresolved_by_reason"] == {
        "call": 1,
        "sqlalchemy_expression": 1,
        "unknown_name": 1,
    }
    code = main(["check-sql", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["unresolved_by_reason"]["call"] == 1
    assert payload["statements"] == 1
