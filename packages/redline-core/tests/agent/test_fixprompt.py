from __future__ import annotations

import json
import time

import pytest

from redline_core import cli
from redline_core.agent.fixprompt import build_fix_prompt
from redline_core.detect import detect
from redline_core.storage.sqlite import SQLiteStore

from ..conftest import make_callsite, make_trace, sql_span

GROUP = {
    "id": "abc123",
    "kind": "n_plus_one",
    "severity": "warning",
    "title": "N+1: 6 queries on items from shop/views.py:41",
    "detail": "The same query ran 6 times.",
    "occurrences": 4,
    "observed_total_ms": 12.5,
    "routes": ["/orders"],
    "fingerprint": "SELECT * FROM items WHERE order_id = ?",
    "callsite": {"file": "shop/views.py", "line": 41, "function": "orders", "code": "x"},
    "suggestion": {"summary": "Use select_related", "code": None, "diff": None},
    "why": {
        "rule": "The same SQL shape ran repeatedly.",
        "observed": "6 matching span(s)",
        "threshold": "5",
        "grouping": "Same fingerprint per trace.",
        "limits": "Sampled.",
        "sampled": True,
    },
}


def test_prompt_contains_location_evidence_and_constraints():
    text = build_fix_prompt(GROUP)
    assert "shop/views.py:41" in text and "/orders" in text
    assert "SELECT * FROM items WHERE order_id = ?" in text
    assert "6 matching span(s)" in text and "Use select_related" in text
    assert "response" in text.lower() and "redline check verify" in text
    assert "n_plus_one" in text


def test_prompt_handles_missing_optional_parts():
    text = build_fix_prompt({"id": "x", "kind": "slow_query", "title": "t", "routes": []})
    assert "slow_query" in text


def test_prompt_is_redacted_and_bounded():
    group = {**GROUP, "detail": "token=sk-ant-" + "a" * 40 + " " + "x" * 50_000}
    text = build_fix_prompt(group)
    assert "sk-ant-" + "a" * 40 not in text
    assert len(text) < 20_000


@pytest.fixture
def store_path(tmp_path):
    path = str(tmp_path / "r.db")
    s = SQLiteStore(path, background=False)
    cs = make_callsite()
    t = make_trace(
        spans=[
            sql_span("SELECT * FROM items WHERE order_id = %s", [i], callsite=cs) for i in range(6)
        ],
        started_at=time.time() - 5,
    )
    t.issues = detect(t)
    s.save(t)
    s.close()
    return path, t.issues[0].id


def test_cli_fix_prompt(store_path, tmp_path, capsys):
    path, issue_id = store_path
    assert cli.main(["fix-prompt", issue_id, "--store", path]) == 0
    assert "redline check verify" in capsys.readouterr().out
    out = tmp_path / "p.md"
    assert cli.main(["fix-prompt", issue_id, "--store", path, "--out", str(out)]) == 0
    assert "n_plus_one" in out.read_text()
    assert cli.main(["fix-prompt", "nope", "--store", path]) == 2
    assert cli.main(["fix-prompt", issue_id, "--store", path + ".missing"]) == 2


def test_check_findings_new_only(tmp_path, capsys):
    from redline_core.check.report import FORMAT, VERSION

    entry = {
        "name": "list",
        "path": "/orders",
        "server": {
            "new_issues": [
                {
                    "kind": "n_plus_one",
                    "title": "N+1 on items",
                    "fingerprint": "fp1",
                    "sample_trace_id": "t1",
                }
            ],
            "fixed_issues": [{"kind": "slow_query", "title": "old", "fingerprint": "fp2"}],
        },
    }
    rep = tmp_path / "r.json"
    rep.write_text(
        json.dumps({"format": FORMAT, "version": VERSION, "id": "r", "requests": [entry]})
    )
    assert cli.main(["check", "findings", str(rep), "--new-only", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert [f["kind"] for f in data["findings"]] == ["n_plus_one"]
    assert "redline check verify" in data["findings"][0]["prompt"]
    clean = tmp_path / "c.json"
    clean.write_text(json.dumps({"format": FORMAT, "version": VERSION, "id": "r", "requests": []}))
    assert cli.main(["check", "findings", str(clean), "--new-only"]) == 0
    assert cli.main(["check", "findings", str(tmp_path / "no.json")]) == 2
