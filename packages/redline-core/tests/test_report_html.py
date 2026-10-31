from __future__ import annotations

import io
import re
from argparse import ArgumentParser

import pytest

from redline_core import cli
from redline_core.model import Issue, LogRecord, Span, Trace
from redline_core.redact import Redactor
from redline_core.report_html import render_run, render_trace
from redline_core.runs import Run, RunStep
from redline_core.storage import SQLiteStore


def make_trace(trace_id="rec-1"):
    sql = Span(
        id="s1",
        parent_id=None,
        kind="sql",
        name="SELECT order",
        start_ms=2.0,
        duration_ms=8.0,
        data={
            "sql": "SELECT * FROM shop_order WHERE token = 'hunter2secret'",
            "fingerprint": "SELECT * FROM shop_order WHERE token = ?",
            "params": ["hunter2secret"],
        },
    )
    issue = Issue(
        id="i1",
        kind="n_plus_one",
        severity="warning",
        title="N+1 <script>alert(1)</script>",
        detail="password=hunter2secret",
        count=7,
        total_ms=12.0,
    )
    return Trace(
        id=trace_id,
        framework="other",
        method="GET",
        path="/orders",
        route="/orders/<id>",
        status=200,
        started_at=1_700_000_000.0,
        duration_ms=30.0,
        query_count=1,
        query_ms=8.0,
        spans=[sql],
        issues=[issue],
        request={"headers": {"authorization": "Bearer topsecretvalue"}, "body": "bodysecret"},
        logs=[LogRecord(t_ms=1.0, level="INFO", logger="shop", message="api_key=abc123secret")],
    )


def test_trace_page_is_self_contained_and_escaped():
    page = render_trace(make_trace(), Redactor())
    assert page.startswith("<!doctype html>")
    assert "<script" not in page.lower().replace("&lt;script&gt;", "")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert not re.search(r"(src|href)=|@import|url\(", page)
    assert "Content-Security-Policy" in page and "default-src 'none'" in page
    assert "N+1" in page and "shop_order" in page and "rec-1" in page


def test_secrets_bodies_and_params_never_appear():
    page = render_trace(make_trace(), Redactor())
    for secret in ("hunter2secret", "topsecretvalue", "bodysecret", "abc123secret"):
        assert secret not in page


def test_sql_text_is_the_fingerprint_unless_asked():
    assert "token = ?" in render_trace(make_trace(), Redactor())
    raw = render_trace(make_trace(), Redactor(), include_sql=True)
    assert "hunter2secret" not in raw  # still redacted


def make_run():
    step = RunStep(step=0, target_rps=10, sent=100, ok=95, errors=5, error_breakdown={"500": 5})
    return Run(
        id="run-1",
        name="smoke <b>",
        status="done",
        config={"requests": [{"method": "GET", "path": "/orders?token=zzzsecretzzz"}]},
        steps=[step],
        summary={"verdict": "ok"},
    )


def test_run_page():
    page = render_run(make_run(), Redactor())
    assert "run-1" in page and "smoke &lt;b&gt;" in page and "<b>" not in page
    assert "zzzsecretzzz" not in page and "500" in page


@pytest.fixture
def store_path(tmp_path):
    path = tmp_path / "r.db"
    s = SQLiteStore(str(path), background=False)
    s.save(make_trace())
    s.save_run(make_run())
    s.close()
    return path


def run_export(*argv):
    parser = ArgumentParser()
    cli.COMMANDS["export"][1](parser)
    out, err = io.StringIO(), io.StringIO()
    code = cli.COMMANDS["export"][2](parser.parse_args(list(argv)), out, err)
    return code, out.getvalue(), err.getvalue()


def test_cli_exports_a_trace(store_path, tmp_path):
    target = tmp_path / "out.html"
    code, out, _ = run_export("rec-1", "--html", str(target), "--store", str(store_path))
    assert code == 0 and "wrote" in out
    page = target.read_text()
    assert "/orders/&lt;id&gt;" in page and "hunter2secret" not in page


def test_cli_exports_a_run(store_path, tmp_path):
    target = tmp_path / "run.html"
    code, _, _ = run_export("run-1", "--html", str(target), "--store", str(store_path))
    assert code == 0 and "Load run" not in target.read_text() and "smoke" in target.read_text()


def test_cli_errors(store_path, tmp_path):
    t = str(tmp_path / "o.html")
    assert run_export("nope", "--html", t, "--store", str(store_path))[0] == 1
    assert not (tmp_path / "o.html").exists()
    assert run_export("--html", t, "--store", str(store_path))[0] == 2
    assert (
        run_export(
            "rec-1", "--html", t, "--send", "--endpoint", "http://x", "--store", str(store_path)
        )[0]
        == 2
    )
    assert run_export("rec-1", "--store", str(store_path))[0] == 2


def test_existing_otlp_export_is_unchanged(store_path):
    import json

    code, out, _ = run_export("--store", str(store_path))
    data = json.loads(out)
    assert code == 0 and set(data) == {"spans", "logs"}
