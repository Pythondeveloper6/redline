from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from flask_redline import SQLAlchemyFixSuggester

from . import shop_app

ROOT = Path(__file__).resolve().parent.parent


def n_plus_one(trace):
    issues = [i for i in trace.issues if i.kind == "n_plus_one"]
    assert len(issues) == 1, [i.title for i in trace.issues]
    return issues[0]


def run(env, path):
    assert env.client().get(path).status_code == 200
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


def test_collection_n_plus_one_selectinload(make_env, tmp_path):
    t = run(make_env(), "/nplus1/items")
    issue = n_plus_one(t)
    s = issue.suggestion
    assert s.confidence == "high"
    assert s.code == "select(Order).options(selectinload(Order.items))"
    assert issue.callsite.file == "tests/shop_app.py"
    assert re.search(
        r"^\+\s+orders = \(s\.scalars\(select\(Order\)\.options\(selectinload\(Order\.items\)\)\)\)"
        r"\.all\(\)$",
        s.diff,
        re.M,
    ), s.diff
    assert "--- a/tests/shop_app.py" in s.diff and "+++ b/tests/shop_app.py" in s.diff
    patched = apply_diff(s.diff, tmp_path)
    assert re.search(r"^\s*selectinload,?$|import .*selectinload", patched, re.M)


def test_many_to_one_joinedload(make_env, tmp_path):
    t = run(make_env(), "/nplus1/customers")
    s = n_plus_one(t).suggestion
    assert s.confidence == "high"
    assert s.code == "select(Order).options(joinedload(Order.customer))"
    assert "order_by(Order.id).options(joinedload(Order.customer))" in s.diff
    patched = apply_diff(s.diff, tmp_path)
    assert "joinedload" in patched.split("class Base")[0]  # import added


def test_lazy_one_to_many_with_query_api(make_env, tmp_path):
    t = run(make_env(), "/nplus1/lazy-items")
    s = n_plus_one(t).suggestion
    assert s.confidence == "high"
    # `.all()` is terminal: options must go before it
    assert "query(Order).order_by(Order.id).options(selectinload(Order.items)).all()" in s.diff
    apply_diff(s.diff, tmp_path)


def test_issue_reaches_the_api(make_env):
    env = make_env()
    run(env, "/nplus1/items")
    c = env.client()
    issues = c.get("/_redline/api/issues?kind=n_plus_one").json["items"]
    assert issues and "selectinload" in issues[0]["suggestion"]["code"]
    assert issues[0]["suggestion"]["diff"]


def test_duplicate_queries_detected(make_env):
    t = run(make_env(), "/dup")
    dups = [i for i in t.issues if i.kind == "duplicate_query"]
    assert len(dups) == 1 and dups[0].count == 2
    assert dups[0].suggestion is not None  # generic fallback advice


def test_suggester_is_registered_and_explicit_models_work(make_env):
    env = make_env()
    assert isinstance(env.redline.suggester, SQLAlchemyFixSuggester)
    explicit = SQLAlchemyFixSuggester(shop_app.Base, project_root=str(ROOT))
    t = run(env, "/nplus1/items")
    issue = n_plus_one(t)
    spans = [s for s in t.spans if s.id in issue.span_ids]
    assert explicit.suggest(issue, spans).code == issue.suggestion.code
