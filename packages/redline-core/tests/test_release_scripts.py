"""The release-verification scripts under scripts/ (store upgrade, clean install)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    if not path.is_file():  # an sdist of redline-core alone has no scripts/
        pytest.skip(f"{path} not present")
    spec = importlib.util.spec_from_file_location(f"_script_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_store_upgrade_round_trip(tmp_path, capsys):
    upgrade = _load("check_store_upgrade")
    db = tmp_path / "traces.db"
    assert upgrade.main(["write", str(db)]) == 0
    manifest = json.loads((tmp_path / "traces.db.manifest.json").read_text())
    assert set(manifest["traces"]) == {"upgrade-n1", "upgrade-slow", "upgrade-tagged"}
    assert "n_plus_one" in manifest["traces"]["upgrade-n1"]["issue_kinds"]
    assert upgrade.main(["read", str(db)]) == 0
    assert ": OK" in capsys.readouterr().out


def test_store_upgrade_detects_a_lost_trace(tmp_path, capsys):
    upgrade = _load("check_store_upgrade")
    db = tmp_path / "traces.db"
    assert upgrade.main(["write", str(db)]) == 0
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM traces WHERE id = 'upgrade-slow'")
    conn.commit()
    conn.close()
    assert upgrade.main(["read", str(db)]) == 1
    assert "upgrade-slow: missing after upgrade" in capsys.readouterr().err


def test_store_upgrade_usage_errors(capsys):
    upgrade = _load("check_store_upgrade")
    assert upgrade.main([]) == 2
    assert upgrade.main(["delete", "x.db"]) == 2


def test_clean_install_resolves_local_wheel_requirements(tmp_path):
    clean = _load("check_clean_install")
    for name in ("redline_core-0.5.0a1-py3-none-any.whl", "flask_redline-0.5.0a1-py3-none-any.whl"):
        (tmp_path / name).write_bytes(b"")
    args = clean.argparse.Namespace(wheels=str(tmp_path), version=None)
    reqs = clean._requirement("flask-redline", args)
    assert sorted(Path(r).name for r in reqs) == [
        "flask_redline-0.5.0a1-py3-none-any.whl",
        "redline_core-0.5.0a1-py3-none-any.whl",
    ]
    pypi = clean.argparse.Namespace(wheels=None, version="0.5.0a1")
    assert clean._requirement("django-redline", pypi) == ["django-redline==0.5.0a1"]
    with pytest.raises(SystemExit):
        clean._requirement("litestar-redline", args)


def _load_benchmark():
    path = ROOT / "benchmarks" / "real_app.py"
    if not path.is_file():
        pytest.skip(f"{path} not present")
    spec = importlib.util.spec_from_file_location("_bench_real_app", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_real_app_benchmark_interleaves_and_reports_overhead():
    bench = _load_benchmark()
    calls: list[str] = []

    def fake_fetch(url, headers, timeout):
        calls.append(url)
        assert headers == {"Authorization": "Bearer t"}
        if url.endswith("/boom"):
            return 1.0, False
        return (12.0 if url.startswith("http://b") else 10.0), True

    results = bench.run(
        {"baseline": "http://a/", "candidate": "http://b"},
        ["/orders", "/boom"],
        requests=3,
        concurrency=2,
        warmup=1,
        headers=bench._headers(["Authorization: Bearer t"]),
        timeout=1.0,
        fetcher=fake_fetch,
    )
    # warm-up first, then strictly alternating baseline / candidate per round
    timed = calls[4:]
    assert timed[:4] == ["http://a/orders"] * 2 + ["http://b/orders"] * 2
    data = bench.report(results)
    orders, boom, total = data["rows"]
    assert orders["overhead_p50_ms"] == pytest.approx(2.0)
    assert orders["candidate"]["n"] == 6
    assert boom["candidate"]["errors"] == 6 and boom["candidate"]["n"] == 0
    assert total["path"] == "(all)"
    assert "| /orders | 6 | 10.00 / 10.00 | 12.00 / 12.00 | +2.00 / +2.00 | 0/0 |" in (
        bench.to_markdown(data)
    )


def test_real_app_benchmark_against_live_http_servers():
    import http.server
    import threading

    bench = _load_benchmark()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok" if self.path == "/ok" else b"no"
            self.send_response(200 if self.path == "/ok" else 503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        urls = [f"http://127.0.0.1:{s.server_address[1]}" for s in servers]
        argv = ["--baseline", urls[0], "--candidate", urls[1], "--path", "/ok"]
        assert bench.main([*argv, "--requests", "5", "--warmup", "1"]) == 0
        assert bench.main([*argv, "--path", "/down", "--requests", "2", "--warmup", "0"]) == 1
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_real_app_benchmark_rejects_bad_headers():
    bench = _load_benchmark()
    with pytest.raises(bench.argparse.ArgumentTypeError):
        bench._headers(["no-colon"])
    assert bench.quantile([], 0.5) == 0.0
    assert bench.quantile([1.0, 3.0], 0.5) == 2.0


def test_run_tests_builds_per_package_commands(capsys):
    runner = _load("run_tests")
    cmd = runner.command("flask", cov=True, extra=["-x"])
    assert cmd[1:] == [
        "-m",
        "pytest",
        "-q",
        "--cov=flask_redline",
        "--cov-report=term:skip-covered",
        "-x",
    ]
    with pytest.raises(SystemExit):
        runner.main(["nope"])
    assert "unknown package(s) nope" in capsys.readouterr().err
