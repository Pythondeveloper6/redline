from __future__ import annotations

import json
import sys
import types
from xml.etree import ElementTree as ET

import pytest

import redline_core.check
from redline_core import cli
from redline_core.check import Baseline

pytest.importorskip("httpx")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)


def write_suite(tmp_path, base_url, **extra):
    data = {
        "name": "shop",
        "base_url": base_url,
        "repeat": 3,
        "warmup": 1,
        "requests": [
            {"name": "list", "path": "/orders", "ignore": ["now"]},
            {"name": "health", "path": "/health"},
        ],
    }
    data.update(extra)
    p = tmp_path / "shop.json"
    p.write_text(json.dumps(data))
    return p


def setup_app(app, name="a"):
    app.json("/orders", {"items": [1, 2], "now": 1})
    app.json("/health", {"name": name})


def test_record_then_compare(app, served, tmp_path, capsys):
    setup_app(app)
    suite = write_suite(tmp_path, served)
    assert cli.main(["check", "record", str(suite), "--no-store", "--label", "before"]) == 0
    out = capsys.readouterr().out
    assert "recording a baseline" in out and "wrote baseline" in out
    baseline = tmp_path / "shop.baseline.json"
    b = Baseline.from_file(baseline)
    assert b.label == "before" and [r["name"] for r in b.requests] == ["list", "health"]
    tags = {c[2].split(";")[0] for c in app.calls}
    assert len(tags) == 1 and tags.pop() == b.id

    code = cli.main(
        [
            "check", "compare", str(suite), "--no-store",
            "--json", "r.json", "--markdown", "r.md", "--junit", "r.xml",
        ]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS" in out and "\x1b[" not in out  # not a TTY: no colour
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["format"] == "redline-check-report"
    assert [r["response"]["verdict"] for r in report["requests"]] == ["same", "same"]
    assert (tmp_path / "r.md").read_text().startswith("### Redline refactor check: shop")
    assert ET.parse(tmp_path / "r.xml").getroot().tag == "testsuites"

    setup_app(app, name="b")  # the refactor changed a response
    assert cli.main(["check", "compare", str(suite), "--no-store", "-q"]) == 1
    out = capsys.readouterr().out
    assert "changed" in out and "FAIL: 1 of 2" in out and "~ name:" in out
    assert cli.main(["check", "compare", str(suite), "--no-store", "--fail-on", "slower"]) == 0
    assert cli.main(["check", "compare", str(suite), "--no-store", "--fail-on", "none"]) == 0
    capsys.readouterr()

    assert cli.main(["check", "show", "r.json"]) == 0
    assert "redline check: shop" in capsys.readouterr().out
    assert cli.main(["check", "show", "r.json", "--fail-on", "changed", "--junit", "s.xml"]) == 0
    assert (tmp_path / "s.xml").exists()


def test_run_records_then_compares(app, served, tmp_path, capsys):
    setup_app(app)
    suite = write_suite(tmp_path, served)
    assert cli.main(["check", "run", str(suite), "--no-store", "-q"]) == 0
    assert "recording one" in capsys.readouterr().out
    assert (tmp_path / "shop.baseline.json").exists()
    assert cli.main(["check", "run", str(suite), "--no-store", "-q"]) == 0
    assert "compared with baseline" in capsys.readouterr().out


def test_usage_errors_exit_2(app, served, tmp_path, capsys):
    setup_app(app)
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"name": "x", "requests": [{"name": "w", "path": "/a", "method": "POST"}]})
    )
    assert cli.main(["check", "record", str(bad), "--base-url", served]) == 2
    assert "read-only" in capsys.readouterr().err
    suite = write_suite(tmp_path, served)
    assert cli.main(["check", "compare", str(suite), "--no-store"]) == 2
    assert "no baseline at" in capsys.readouterr().err
    (tmp_path / "shop.baseline.json").write_text(
        json.dumps({"format": "redline-check-baseline", "version": 7})
    )
    assert cli.main(["check", "compare", str(suite), "--no-store"]) == 2
    assert "unsupported baseline version 7" in capsys.readouterr().err
    nobase = tmp_path / "nobase.json"
    nobase.write_text(json.dumps({"requests": [{"path": "/a"}]}))
    assert cli.main(["check", "record", str(nobase)]) == 2
    assert "base_url" in capsys.readouterr().err
    assert cli.main(["check", "record", str(suite), "--base-url", "ftp://x"]) == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "compare", str(suite), "--fail-on", "slow"])
    assert exc.value.code == 2
    assert "unknown verdict" in capsys.readouterr().err
    assert cli.main(["check"]) == 2
    assert "redline check record" in capsys.readouterr().err
    assert cli.main(["check", "show", "missing.json"]) == 2
    (tmp_path / "x.json").write_text("{}")
    assert cli.main(["check", "show", "x.json"]) == 2
    env = tmp_path / "env.json"
    env.write_text(
        json.dumps(
            {"requests": [{"path": "/orders", "headers": {"X-T": "${env:REDLINE_TEST_UNSET_VAR}"}}]}
        )
    )
    assert cli.main(["check", "record", str(env), "--base-url", served, "--no-store"]) == 2
    assert "REDLINE_TEST_UNSET_VAR is not set" in capsys.readouterr().err


def test_unreachable_exit_3(tmp_path, dead_url, capsys):
    suite = write_suite(tmp_path, dead_url, timeout_s=2)
    assert cli.main(["check", "record", str(suite), "--no-store"]) == 3
    assert "could not reach" in capsys.readouterr().err
    assert not (tmp_path / "shop.baseline.json").exists()


def test_threshold_overrides(app, served, tmp_path, capsys):
    setup_app(app)
    suite = write_suite(tmp_path, served)
    assert cli.main(["check", "record", str(suite), "--no-store", "-q"]) == 0
    code = cli.main(
        [
            "check",
            "compare",
            str(suite),
            "--no-store",
            "--slower-pct",
            "0",
            "--slower-ms",
            "0",
            "-q",
            "--json",
            "r.json",
        ]
    )
    assert code in (0, 1)
    assert cli.main(["check", "compare", str(suite), "--no-store", "--slower-ms", "-1"]) == 2


def test_store_without_check_storage(app, served, tmp_path, capsys, monkeypatch):
    from redline_core.storage import SQLiteStore

    SQLiteStore(str(tmp_path / "r.db"), background=False).close()
    monkeypatch.setitem(sys.modules, "redline_core.check.sink", None)  # module missing
    monkeypatch.delattr(redline_core.check, "sink", raising=False)  # also once imported
    setup_app(app)
    suite = write_suite(tmp_path, served)
    assert cli.main(["check", "record", str(suite), "--store", "r.db", "-q"]) == 0
    err = capsys.readouterr().err
    assert "server-side metrics need redline-core with check storage" in err
    assert cli.main(["check", "record", str(suite), "--store", "nope.db", "-q"]) == 0
    assert "no store at nope.db" in capsys.readouterr().err
    assert not (tmp_path / "nope.db").exists()


def test_store_sink_and_baseline_id(app, served, tmp_path, capsys, monkeypatch):
    from redline_core.storage import SQLiteStore

    SQLiteStore(str(tmp_path / "r.db"), background=False).close()
    saved: dict = {}

    class StoreCheckSink:
        def __init__(self, store):
            self.store = store

        def start(self, run):
            saved.setdefault("runs", []).append(run)

        def result(self, run_id, idx, name, data):
            saved.setdefault(run_id, []).append(data)

        def finish(self, run_id, status, summary, error):
            saved[f"{run_id}:status"] = status

    class StoreServerMetrics:
        def __init__(self, store):
            pass

        def for_check(self, run_id, timeout_s=5.0):
            return {0: {"queries_median": 2, "queries_max": 2, "sql": {}, "issues": []}}

    def load_baseline(store, baseline_id):
        return saved.get("baseline")

    module = types.ModuleType("redline_core.check.sink")
    module.StoreCheckSink = StoreCheckSink
    module.StoreServerMetrics = StoreServerMetrics
    module.load_baseline = load_baseline
    monkeypatch.setitem(sys.modules, "redline_core.check.sink", module)
    monkeypatch.setattr(redline_core.check, "sink", module, raising=False)

    setup_app(app)
    suite = write_suite(tmp_path, served)
    assert cli.main(["check", "record", str(suite), "--store", "r.db", "-q"]) == 0
    capsys.readouterr()
    rid = saved["runs"][0]["id"]
    assert saved[f"{rid}:status"] == "done"
    assert saved[rid][0]["server"]["queries_median"] == 2
    saved["baseline"] = Baseline.from_file(tmp_path / "shop.baseline.json").to_dict()
    assert (
        cli.main(["check", "compare", str(suite), "--store", "r.db", "--baseline-id", rid, "-q"])
        == 0
    )
    out = capsys.readouterr().out
    assert "2 → 2" in out  # queries before → after
    saved.pop("baseline")
    assert cli.main(["check", "compare", str(suite), "--store", "r.db", "--baseline-id", "x"]) == 2
    assert "no baseline x" in capsys.readouterr().err


def test_init_from_traces_cli(tmp_path, capsys):
    from redline_core.storage import SQLiteStore

    from ..conftest import make_trace

    db = tmp_path / "r.db"
    store = SQLiteStore(str(db), background=False)
    store.save(make_trace(path="/orders/7/", route="/orders/<int:pk>/"))
    store.close()
    assert cli.main(["check", "init", "--from-traces", "--store", str(db), "-o", "c.json"]) == 0
    data = json.loads((tmp_path / "c.json").read_text())
    assert data["requests"][0]["path"] == "/orders/7/"
    assert cli.main(["check", "init", "--from-traces", "--store", str(db), "-o", "c.json"]) == 2
    assert "exists" in capsys.readouterr().err
    assert cli.main(["check", "init", "--from-traces", "--store", str(db), "-o", "-"]) == 0
    assert "[[requests]]" in capsys.readouterr().out
    assert cli.main(["check", "init", "--from-traces", "--store", "missing.db"]) == 2
