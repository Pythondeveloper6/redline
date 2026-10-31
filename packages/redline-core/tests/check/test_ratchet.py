from __future__ import annotations

import json
import types

import pytest

from redline_core import cli
from redline_core.check import ratchet

pytest_plugins = ["pytester"]


def req(name, queries=None, median=None, broken=False):
    return {
        "name": name,
        "broken": broken,
        "server": {"queries_median": queries} if queries is not None else None,
        "perf": {"median": median} if median is not None else None,
    }


def test_observe_rounds_queries_up_and_marks_broken():
    got = ratchet.observe([req("a", 3.5, 10.0), req("b", broken=True), req("c", median=4.0)])
    assert got["a"] == {"queries": 4, "median_ms": 10.0}
    assert got["b"] == {"broken": True} and got["c"] == {"median_ms": 4.0}
    assert "median_ms" not in ratchet.observe([req("a", 1, 9.0)], latency=False)["a"]


def test_more_queries_is_a_regression_and_never_rewritten():
    recorded = {"a": {"queries": 3}}
    r = ratchet.compare(recorded, {"a": {"queries": 4}})
    assert not r.ok and "4 queries, was 3" in r.regressions[0]
    assert r.updated["a"] == {"queries": 3}


def test_fewer_queries_tightens():
    r = ratchet.compare({"a": {"queries": 5}}, {"a": {"queries": 2}})
    assert r.ok and r.improvements and r.updated["a"] == {"queries": 2}


def test_latency_needs_both_tolerances():
    base = {"a": {"median_ms": 100.0}}
    assert ratchet.compare(base, {"a": {"median_ms": 125.0}}).ok  # +25 ms but < 30 %
    assert ratchet.compare(base, {"a": {"median_ms": 125.0}}, latency_pct=10).ok is False
    assert not ratchet.compare(base, {"a": {"median_ms": 140.0}}).ok
    better = ratchet.compare(base, {"a": {"median_ms": 50.0}})
    assert better.ok and better.updated["a"]["median_ms"] == 50.0
    noise = ratchet.compare(base, {"a": {"median_ms": 95.0}})
    assert noise.updated["a"]["median_ms"] == 100.0 and noise.unchanged == 1


def test_new_missing_and_broken():
    r = ratchet.compare({"gone": {"queries": 1}}, {"new": {"queries": 2}, "b": {"broken": True}})
    assert r.new == ["new: 2 queries"] and r.missing == ["gone"]
    assert not r.ok and r.broken
    assert set(r.updated) == {"gone", "new"}


def test_write_is_deterministic_and_keeps_other_section(tmp_path):
    path = str(tmp_path / "r.json")
    ratchet.write(path, {"z": {"queries": 1}, "a": {"queries": 2, "median_ms": 1.234}})
    first = open(path).read()
    assert first.index('"a"') < first.index('"z"') and first.endswith("}\n")
    ratchet.write(path, {"t::x": {"queries": 7}}, "tests")
    assert ratchet.load(path)["a"] == {"queries": 2, "median_ms": 1.2}
    assert ratchet.load(path, "tests") == {"t::x": {"queries": 7}}
    ratchet.write(path, {"z": {"queries": 1}, "a": {"queries": 2, "median_ms": 1.234}})
    assert json.loads(open(path).read())["tests"]


def test_load_rejects_foreign_files(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"hello": 1}')
    with pytest.raises(ratchet.RatchetError):
        ratchet.load(str(p))
    with pytest.raises(ratchet.RatchetError):
        ratchet.write(str(p), {})
    assert p.read_text() == '{"hello": 1}'
    with pytest.raises(FileNotFoundError):
        ratchet.load(str(tmp_path / "none.json"))


# --------------------------------------------------------------------------- CLI
@pytest.fixture
def fake_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    suite = tmp_path / "shop.json"
    suite.write_text(
        json.dumps(
            {
                "name": "shop",
                "base_url": "http://127.0.0.1:1",
                "requests": [{"name": "orders", "path": "/orders"}],
            }
        )
    )
    state = {"requests": [req("orders", 4, 10.0)]}

    def fake(args, out, **kw):
        return types.SimpleNamespace(status="done", requests=state["requests"])

    monkeypatch.setattr(cli, "_run_check", fake)
    return suite, state


def test_cli_requires_update_to_create(fake_run, capsys):
    suite, _ = fake_run
    assert cli.main(["check", "ratchet", str(suite), "--no-store"]) == 2
    assert not (suite.parent / "shop.ratchet.json").exists()
    assert cli.main(["check", "ratchet", str(suite), "--no-store", "--update"]) == 0
    data = ratchet.load(str(suite.parent / "shop.ratchet.json"))
    assert data == {"orders": {"queries": 4, "median_ms": 10.0}}


def test_cli_regression_fails_and_keeps_file(fake_run, capsys):
    suite, state = fake_run
    cli.main(["check", "ratchet", str(suite), "--no-store", "--update"])
    path = suite.parent / "shop.ratchet.json"
    before = path.read_text()
    state["requests"] = [req("orders", 9, 10.0)]
    code = cli.main(["check", "ratchet", str(suite), "--no-store", "--update", "--json", "v.json"])
    assert code == 1 and path.read_text() == before
    assert json.loads((suite.parent / "v.json").read_text())["ok"] is False


def test_cli_improvement_tightens_only_with_update(fake_run):
    suite, state = fake_run
    cli.main(["check", "ratchet", str(suite), "--no-store", "--update"])
    path = suite.parent / "shop.ratchet.json"
    state["requests"] = [req("orders", 2, 10.0)]
    assert cli.main(["check", "ratchet", str(suite), "--no-store"]) == 0
    assert ratchet.load(str(path))["orders"]["queries"] == 4
    assert cli.main(["check", "ratchet", str(suite), "--no-store", "--update"]) == 0
    assert ratchet.load(str(path))["orders"]["queries"] == 2


# --------------------------------------------------------------------------- pytest plugin
TEST_FILE = """
import pytest
from redline_core.conf import Settings
from redline_core.recorder import Recorder, record_query

@pytest.mark.redline()
def test_list():
    recorder = Recorder(Settings())
    builder = recorder.start_trace(method="GET", path="/orders")
    for _ in range(QUERIES):
        record_query("SELECT 1", [], start=builder.t0, duration=0.001)
    recorder.finish_trace(builder, status=200)
"""


def _run(pytester, queries, *args):
    pytester.makepyfile(test_orders=TEST_FILE.replace("QUERIES", str(queries)))
    return pytester.runpytest_subprocess(*args)


def test_plugin_ratchet_creates_tightens_and_fails(pytester, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")  # rewritten file, same size and second
    path = pytester.path / "r.json"
    res = _run(pytester, 3, "--redline-ratchet", str(path), "--redline-ratchet-update")
    res.assert_outcomes(passed=1)
    key = "test_orders.py::test_list"
    assert ratchet.load(str(path), "tests") == {key: {"queries": 3}}

    res = _run(pytester, 5, "--redline-ratchet", str(path), "--redline-ratchet-update")
    res.assert_outcomes(failed=1)
    res.stdout.fnmatch_lines(["*Redline ratchet: 5 queries, but 3 are recorded*"])
    assert ratchet.load(str(path), "tests")[key] == {"queries": 3}

    res = _run(pytester, 1, "--redline-ratchet", str(path))  # no update: file untouched
    res.assert_outcomes(passed=1)
    assert ratchet.load(str(path), "tests")[key] == {"queries": 3}
    res = _run(pytester, 1, "--redline-ratchet", str(path), "--redline-ratchet-update")
    res.assert_outcomes(passed=1)
    assert ratchet.load(str(path), "tests")[key] == {"queries": 1}
