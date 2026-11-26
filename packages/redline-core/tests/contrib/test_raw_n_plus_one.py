"""N+1 written in raw SQL gets a real batched rewrite, and it verifies."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import textwrap

import pytest
from sqlalchemy import create_engine, text

from redline_core.callsite import CallsiteCapturer
from redline_core.conf import Settings
from redline_core.contrib.sqlalchemy import SQLAlchemyFixSuggester, instrument_engine
from redline_core.contrib.sqlalchemy.rawfix import batched_sql, param_limit
from redline_core.detect import Detector

SOURCE = textwrap.dedent(
    """\
    from sqlalchemy import text


    def load(conn, orders):
        out = {}
        for order in orders:
            rows = conn.execute(
                text("SELECT id, qty, order_id FROM items WHERE order_id = :oid"), {"oid": order.id}
            ).all()
            out[order.id] = [tuple(r) for r in rows]
        return out
    """
)


class Order:
    def __init__(self, id):
        self.id = id


def import_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path, monkeypatch, tracer):
    monkeypatch.chdir(tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    instrument_engine(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY, order_id INT, qty INT)"))
        for i in range(1, 31):
            conn.execute(
                text("INSERT INTO items (order_id, qty) VALUES (:o, :q)"), {"o": i, "q": i}
            )
            conn.execute(
                text("INSERT INTO items (order_id, qty) VALUES (:o, :q)"), {"o": i, "q": 100}
            )
    (tmp_path / "rawshop.py").write_text(SOURCE)
    previous = tracer.recorder.capturer
    tracer.recorder.capturer = CallsiteCapturer(project_root=str(tmp_path))  # the project
    yield tmp_path, engine
    tracer.recorder.capturer = previous
    sys.modules.pop("rawshop", None)


def run_loop(tracer, tmp_path, engine):
    module = import_module(tmp_path / "rawshop.py")
    builder = tracer.start()
    with engine.connect() as conn:
        result = module.load(conn, [Order(i) for i in range(1, 31)])
    return result, tracer.recorder.finish_trace(builder, status=200)


def detect(trace, tmp_path):
    detector = Detector(Settings(), [SQLAlchemyFixSuggester(project_root=str(tmp_path))])
    return detector.detect(trace)


def test_the_same_text_select_30_times_yields_a_diff_that_verifies(tracer, project):
    tmp_path, engine = project
    before, trace = run_loop(tracer, tmp_path, engine)
    issue = next(i for i in detect(trace, tmp_path) if i.kind == "n_plus_one")
    suggestion = issue.suggestion
    assert suggestion.diff and suggestion.confidence == "high"
    assert "IN :_ids" in suggestion.diff and "expanding=True" in suggestion.diff
    assert "sqlite allows about 32,766" in suggestion.summary
    if shutil.which("patch") is None:
        pytest.skip("patch not available")
    applied = subprocess.run(
        ["patch", "-p1", "--quiet"], input=suggestion.diff, text=True, cwd=tmp_path
    )
    assert applied.returncode == 0
    sys.modules.pop("rawshop", None)
    after, trace2 = run_loop(tracer, tmp_path, engine)
    assert after == before  # same data, grouped back per parent
    assert [i for i in detect(trace2, tmp_path) if i.kind == "n_plus_one"] == []
    assert len([s for s in trace2.spans if s.kind == "sql"]) == 1


def test_batched_sql_rewrites_only_what_it_can():
    sql, col, bind, added = batched_sql(
        "SELECT id, qty FROM items WHERE order_id = :oid AND qty > 1"
    )
    assert sql == "SELECT id, qty, order_id FROM items WHERE order_id IN :_ids AND qty > 1"
    assert (col, bind, added) == ("order_id", "oid", True)
    assert batched_sql("SELECT order_id, qty FROM items WHERE order_id = :o")[3] is False
    assert batched_sql("SELECT * FROM items WHERE order_id = :o")[0].endswith("IN :_ids")
    assert batched_sql("SELECT * FROM items WHERE order_id = :o LIMIT 1") is None
    assert batched_sql("UPDATE items SET qty = 1 WHERE order_id = :o") is None


def test_limits_per_database():
    assert param_limit("mssql") == 2100 and param_limit("sqlite") > 30_000
    assert param_limit("something-else") == 1000


def test_a_dynamic_statement_gets_the_code_block_not_a_diff(tracer, project):
    tmp_path, engine = project
    dynamic = SOURCE.replace(
        'text("SELECT id, qty, order_id FROM items WHERE order_id = :oid")',
        'text("SELECT id, qty, order_id FROM items " + "WHERE order_id = :oid")',
    )
    (tmp_path / "rawshop.py").write_text(dynamic)  # not one literal: no rewrite is attempted
    _, trace = run_loop(tracer, tmp_path, engine)
    issue = next(i for i in detect(trace, tmp_path) if i.kind == "n_plus_one")
    assert issue.suggestion.diff is None
    assert "bindparam" in issue.suggestion.code
    assert "32,766" in issue.suggestion.summary
