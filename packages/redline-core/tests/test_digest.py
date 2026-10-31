from __future__ import annotations

import io
import json
import threading
import time
from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from redline_core import cli
from redline_core.model import Issue, Trace
from redline_core.storage import SQLiteStore


def trace(trace_id, issues, started_at=None):
    return Trace(
        id=trace_id,
        framework="other",
        method="GET",
        path="/orders",
        route="/orders",
        status=200,
        started_at=time.time() if started_at is None else started_at,
        duration_ms=10.0,
        issues=issues,
    )


def issue(issue_id, kind="n_plus_one", severity="warning", title="Repeated query"):
    return Issue(
        id=issue_id, kind=kind, severity=severity, title=title, detail="", count=6, total_ms=5.0
    )


@pytest.fixture
def store_path(tmp_path):
    path = tmp_path / "r.db"
    s = SQLiteStore(str(path), background=False)
    s.save(trace("t1", [issue("fp-a", title="N+1 <orders> password=hunter2secret")]))
    s.save(trace("t2", [issue("fp-b", "sql_error", "critical", "boom")]))
    s.save(trace("t3", [issue("fp-old")], started_at=time.time() - 5 * 86_400))
    s.close()
    return path


class Hook:
    def __init__(self, status=200):
        self.bodies: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                outer.bodies.append(json.loads(self.rfile.read(n)))
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hook():
    made = []

    def make(**kw):
        made.append(Hook(**kw))
        return made[-1]

    yield make
    for h in made:
        h.close()


def run(*argv):
    parser = ArgumentParser()
    cli.COMMANDS["digest"][1](parser)
    out, err = io.StringIO(), io.StringIO()
    code = cli.COMMANDS["digest"][2](parser.parse_args(list(argv)), out, err)
    return code, out.getvalue(), err.getvalue()


def test_posts_new_issues_worst_first_and_redacted(store_path, hook):
    h = hook()
    code, out, _ = run("--store", str(store_path), "--webhook", h.url, "--since", "24h")
    assert code == 0 and "sent 2" in out
    (body,) = h.bodies
    assert set(body) == {"text"}
    lines = body["text"].splitlines()
    assert "2 new issues" in lines[0]
    assert "[critical] sql_error" in lines[1] and "[warning] n_plus_one" in lines[2]
    assert "hunter2secret" not in body["text"] and "<orders>" not in body["text"]
    assert "fp-old" not in body["text"] and "Repeated" not in body["text"]  # outside the window


def test_state_file_prevents_repeats(store_path, hook):
    h = hook()
    args = ("--store", str(store_path), "--webhook", h.url)
    run(*args)
    code, out, _ = run(*args)
    assert code == 0 and "no new issues" in out and len(h.bodies) == 1
    state = json.loads((store_path.parent / "digest-state.json").read_text())
    assert set(state["sent"]) == {"fp-a", "fp-b"}

    s = SQLiteStore(str(store_path), background=False)
    s.save(trace("t9", [issue("fp-new", title="fresh")]))
    s.save(trace("t10", [issue("fp-a")]))  # an already reported issue again
    s.close()
    run(*args)
    assert len(h.bodies) == 2
    assert "1 new issue " in h.bodies[1]["text"] and "fresh" in h.bodies[1]["text"]


def test_failed_delivery_records_nothing(store_path, hook):
    h = hook(status=500)
    code, _, err = run("--store", str(store_path), "--webhook", h.url)
    assert code == 1 and "HTTP 500" in err
    assert not (store_path.parent / "digest-state.json").exists()


def test_dry_run_prints_and_sends_nothing(store_path, hook):
    h = hook()
    code, out, _ = run("--store", str(store_path), "--webhook", h.url, "--dry-run")
    assert code == 0 and json.loads(out)["text"].startswith("*Redline*")
    assert h.bodies == [] and not (store_path.parent / "digest-state.json").exists()
    assert run("--store", str(store_path), "--dry-run")[0] == 0  # no webhook needed


def test_min_severity_and_custom_state(store_path, hook, tmp_path):
    h = hook()
    state = tmp_path / "s" / "state.json"
    code, _, _ = run(
        "--store", str(store_path), "--webhook", h.url, "--min-severity", "critical",
        "--state", str(state), "--ui-url", "https://dev.example.com/_redline",
    )  # fmt: skip
    assert code == 0
    assert "1 new issue " in h.bodies[0]["text"]
    assert h.bodies[0]["text"].endswith("https://dev.example.com/_redline/#/issues")
    assert set(json.loads(state.read_text())["sent"]) == {"fp-b"}


def test_usage_errors(store_path, tmp_path):
    assert run("--store", str(store_path))[0] == 2  # no webhook
    assert run("--store", str(store_path), "--dry-run", "--since", "soon")[0] == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert run("--store", str(store_path), "--dry-run", "--state", str(bad))[0] == 2
    assert run("--store", str(store_path), "--webhook", "ftp://x")[0] == 1
    assert run("--store", str(tmp_path / "none.db"), "--dry-run")[0] == 1


def test_truncates_long_lists(store_path):
    s = SQLiteStore(str(store_path), background=False)
    for i in range(25):
        s.save(trace(f"x{i}", [issue(f"many-{i}")]))
    s.close()
    code, out, _ = run("--store", str(store_path), "--dry-run")
    text = json.loads(out)["text"]
    assert code == 0 and "and 7 more" in text
