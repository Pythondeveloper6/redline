from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fastapi_redline.suggest import SQLAlchemyFixSuggester

from . import shop_app

ROOT = Path(__file__).resolve().parent.parent


def n_plus_one(trace):
    issues = [i for i in trace.issues if i.kind == "n_plus_one"]
    assert len(issues) == 1, [i.title for i in trace.issues]
    return issues[0]


def run(env, path):
    with TestClient(env.app) as c:
        assert c.get(path).status_code == 200
    return env.trace()


def apply_diff(diff: str, tmp_path: Path) -> str:
    """Apply `diff` to a copy of tests/shop_app.py and return the patched source."""
    target = tmp_path / "tests" / "shop_app.py"
    target.parent.mkdir(exist_ok=True)
    target.write_text((ROOT / "tests" / "shop_app.py").read_text())
    subprocess.run(["patch", "-p1", "-s"], input=diff, text=True, cwd=tmp_path, check=True)
    patched = target.read_text()
    ast.parse(patched)  # still valid Python
    return patched


def test_collection_n_plus_one_async_selectinload(make_env, tmp_path):
    t = run(make_env(), "/nplus1/items")
    issue = n_plus_one(t)
    s = issue.suggestion
    assert s.confidence == "high"
    assert s.code == "select(Order).options(selectinload(Order.items))"
    assert "+    selectinload,\n" in s.diff  # joined the existing parenthesised import
    assert re.search(
        r"^\+\s+orders = \(await s\.scalars\(select\(Order\)"
        r"\.options\(selectinload\(Order\.items\)\)\)\)\.all\(\)$",
        s.diff,
        re.M,
    ), s.diff
    assert "--- a/tests/shop_app.py" in s.diff and "+++ b/tests/shop_app.py" in s.diff
    patched = apply_diff(s.diff, tmp_path)
    assert "selectinload" in patched
    assert re.search(r"^\s+selectinload,$", patched, re.M)


def test_many_to_one_async_joinedload(make_env, tmp_path):
    t = run(make_env(), "/nplus1/customers")
    s = n_plus_one(t).suggestion
    assert s.confidence == "high"
    assert s.code == "select(Order).options(joinedload(Order.customer))"
    assert ".options(joinedload(Order.customer))" in s.diff
    assert "order_by(Order.id).options(" in s.diff  # appended to the end of the chain
    patched = apply_diff(s.diff, tmp_path)
    assert "joinedload" in patched.split("class Base")[0]  # import added


def test_lazy_one_to_many_sync_with_query_api(make_env, tmp_path):
    t = run(make_env(), "/nplus1/lazy-items")
    s = n_plus_one(t).suggestion
    assert s.confidence == "high"
    # `.all()` is terminal: options must go before it
    assert "query(Order).order_by(Order.id).options(selectinload(Order.items)).all()" in s.diff
    apply_diff(s.diff, tmp_path)


def test_lazy_many_to_one_sync(make_env, tmp_path):
    t = run(make_env(), "/nplus1/lazy-customers")
    s = n_plus_one(t).suggestion
    assert s.code == "select(Order).options(joinedload(Order.customer))"
    assert "s.scalars(select(Order).options(joinedload(Order.customer)))" in s.diff
    apply_diff(s.diff, tmp_path)


def test_many_to_many_secondary(make_env, tmp_path):
    t = run(make_env(), "/nplus1/tags")
    s = n_plus_one(t).suggestion
    assert s.code == "select(Order).options(selectinload(Order.tags))"
    assert s.confidence == "high"
    assert "select(Order).options(selectinload(Order.tags))" in s.diff
    apply_diff(s.diff, tmp_path)


def test_duplicate_queries_detected(make_env):
    t = run(make_env(), "/dup")
    dups = [i for i in t.issues if i.kind == "duplicate_query"]
    assert len(dups) == 1 and dups[0].count == 2
    assert dups[0].suggestion is not None  # generic fallback advice


def test_code_hint_when_statement_not_in_source(make_env):
    env = make_env()
    run(env, "/nplus1/items")
    suggester = SQLAlchemyFixSuggester(shop_app.Base, project_root=str(ROOT))
    t = env.trace()
    issue = n_plus_one(t)
    spans = [s for s in t.spans if s.id in issue.span_ids]
    # callsite pointing at a file that does not exist -> no diff, lower confidence
    from redline_core.model import Callsite, Frame

    cs = Callsite("nowhere.py", 1, "f", "x", (Frame("nowhere.py", 1, "f"),))
    issue.callsite = cs
    spans[0].callsite = cs
    s = suggester.suggest(issue, spans)
    assert s.diff is None
    assert s.confidence == "medium"
    assert s.code == "select(Order).options(selectinload(Order.items))"


def test_unknown_table_returns_none():
    from redline_core.model import Issue

    suggester = SQLAlchemyFixSuggester(shop_app.Base, project_root=str(ROOT))
    from redline_core.model import Span

    span = Span("a", None, "sql", "x", 0, 1, None, {"sql": "SELECT * FROM mystery WHERE x = ?"})
    issue = Issue("i", "n_plus_one", "warning", "t", "d", ["a"])
    assert suggester.suggest(issue, [span]) is None


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "order_items".id FROM "order_items" WHERE "order_items".order_id = ?',
        "SELECT order_items.id FROM order_items AS oi_1 WHERE ? = order_items.order_id",
        "SELECT oi.id FROM order_items oi WHERE oi.order_id = $1".replace("$1", "?"),
    ],
)
def test_sql_shapes_resolve_to_relationship(sql):
    from redline_core.model import Issue, Span

    suggester = SQLAlchemyFixSuggester(shop_app.Base, project_root=str(ROOT))
    span = Span("a", None, "sql", "x", 0, 1, None, {"sql": sql})
    issue = Issue("i", "n_plus_one", "warning", "t", "d", ["a"])
    s = suggester.suggest(issue, [span])
    assert s is not None and "selectinload(Order.items)" in s.code


@pytest.mark.parametrize(
    ("source", "expected_import_line"),
    [
        # no sqlalchemy.orm import: added after the last sqlalchemy import, before local ones
        ("from sqlalchemy import select\nfrom .x import y\n\nselect(Order)\n", 2),
        # single-line import is extended
        ("from sqlalchemy.orm import Session\n\nselect(Order)\n", 1),
        # already imported: untouched
        ("from sqlalchemy.orm import Session, selectinload\n", 1),
        # no imports at all
        ('"""doc"""\n\nselect(Order)\n', 2),
    ],
)
def test_ensure_import_placement(source, expected_import_line):
    from fastapi_redline.suggest import _ensure_import

    lines = source.splitlines(keepends=True)
    out = _ensure_import(ast.parse(source), lines, "selectinload")
    text = "".join(out)
    ast.parse(text)
    assert text.count("selectinload") == 1
    assert "selectinload" in out[expected_import_line - 1]
