from __future__ import annotations

from redline_core.check_sql import extract


def test_text_clause_constant_and_known_wrappers_resolve():
    source = """
from sqlalchemy import text
_SQL = (
    text("SELECT id FROM things WHERE id = :id")
    .bindparams(id=1)
    .execution_options(stream_results=True)
)
def run(conn):
    return conn.execute(_SQL)
"""
    statements, skipped = extract(source)
    assert not skipped
    assert [item["sql"] for item in statements] == ["SELECT id FROM things WHERE id = :id"]


def test_pure_literal_sql_helper_is_resolved_without_execution():
    source = '''
PREFIX = "SELECT id FROM"

def make_sql(table, predicate="active = 1"):
    """Build a query."""
    return f"{PREFIX} {table} WHERE {predicate}"

def run(conn):
    return conn.execute(make_sql("widgets", predicate="enabled = 1"))
'''
    statements, skipped = extract(source)
    assert not skipped
    assert [item["sql"] for item in statements] == ["SELECT id FROM widgets WHERE enabled = 1"]


def test_helpers_with_side_effects_or_decorators_are_unresolved():
    source = """
def make_sql(table):
    log(table)
    return f"SELECT * FROM {table}"

@decorator
def other_sql(table):
    return f"SELECT * FROM {table}"

conn.execute(make_sql("widgets"))
conn.execute(other_sql("widgets"))
"""
    statements, skipped = extract(source)
    assert not statements
    assert len(skipped) == 2
    assert all(item["reason"] == "dynamic SQL" for item in skipped)


def test_helper_call_uses_only_callsite_literals_and_no_local_shadowing():
    source = """
def make_sql():
    return f"SELECT * FROM {table}"

def run(conn):
    table = "widgets"
    conn.execute(make_sql())
"""
    statements, skipped = extract(source)
    assert not statements
    assert skipped and skipped[0]["reason"] == "dynamic SQL"


def test_non_sqlalchemy_text_binding_and_orm_expressions_remain_unresolved():
    source = """
query = make_text()
conn.execute(query)
conn.execute(select(User).where(User.id == 1))
"""
    statements, skipped = extract(source)
    assert not statements
    assert len(skipped) == 2


def test_assigned_text_declaration_remains_a_candidate_without_execute():
    statements, skipped = extract('Q = text("SELECT 1 -- :not_bound")\n')
    assert not skipped
    assert [item["sql"] for item in statements] == ["SELECT 1 -- :not_bound"]


def test_executed_text_clause_is_not_duplicated_at_use_site():
    source = """
Q = text("SELECT 1 -- :not_bound")
conn.execute(Q)
"""
    statements, skipped = extract(source)
    assert not skipped
    assert len(statements) == 1
    assert statements[0]["line"] == 2


def test_reassigned_or_shadowed_sql_helpers_stay_unresolved():
    source = """
def query():
    return "SELECT 1"

async def run(query):
    await session.execute(query())

query = lambda: "SELECT 2"
conn.execute(query())
"""
    statements, skipped = extract(source)
    assert not statements
    assert len(skipped) == 2


def test_helpers_reject_posonly_keywords_but_accept_keyword_with_global_constant():
    source = """
PREFIX = "SELECT * FROM"

def query(table, /):
    return f"{PREFIX} {table}"

conn.execute(query(table="widgets"))
"""
    statements, skipped = extract(source)
    assert not statements
    assert skipped and skipped[0]["reason"] == "dynamic SQL"


def test_helper_keyword_override_is_allowed_when_global_has_same_name():
    source = """
table = "ignored"

def query(table):
    return f"SELECT * FROM {table}"

conn.execute(query(table="widgets"))
"""
    statements, skipped = extract(source)
    assert not skipped
    assert [item["sql"] for item in statements] == ["SELECT * FROM widgets"]


def test_joined_sql_enforces_total_size_before_joining():
    source = 'BIG = "' + ("x" * 120_000) + '"\nSQL = f"{BIG}{BIG}"\nconn.execute(SQL)\n'
    statements, skipped = extract(source)
    assert not statements
    assert skipped and skipped[0]["reason"] == "dynamic SQL"


def test_known_orm_expression_is_explicitly_unvalidated():
    source = """
from sqlalchemy import select as choose
conn.execute(choose(Item).where(Item.id == 1))
"""
    statements, skipped = extract(source)
    assert not statements
    assert "SQLAlchemy expression" in skipped[0]["reason"]
    assert "runtime capture" in skipped[0]["reason"]


def test_unrelated_method_is_not_evaluated_as_pure_sql_helper():
    statements, skipped = extract(
        'def query(): return "SELECT 1"\nconn.execute(unrelated.query())\n'
    )
    assert not statements and skipped
