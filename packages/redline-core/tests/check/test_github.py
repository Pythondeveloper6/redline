from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from redline_core import cli
from redline_core.check import github

from .test_report import sample_report

ACTION = Path(__file__).parents[4] / "action.yml"


def test_marker_is_sanitised_and_first_line():
    assert github.marker("a b<>") == "<!-- redline-sticky-comment:ab -->"
    body = github.sticky_body("### Title\n\ntext\n", "suite-1")
    assert body.splitlines()[0] == "<!-- redline-sticky-comment:suite-1 -->"
    assert body.endswith("text\n")


def test_body_is_truncated_below_the_comment_limit():
    body = github.sticky_body("x" * 100_000)
    assert len(body) < 65_536 and "truncated" in body


def test_pull_request_number(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7}}))
    env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event)}
    assert github.pull_request_number(env) == 7
    assert github.pull_request_number({**env, "GITHUB_EVENT_NAME": "push"}) is None
    assert github.pull_request_number({"GITHUB_EVENT_NAME": "pull_request"}) is None


class FakeGitHub:
    def __init__(self, existing=(), status=200):
        self.comments = list(existing)
        self.calls: list[tuple[str, str, dict | None]] = []
        self.status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self):
                outer.calls.append(("GET", self.path, None))
                self._reply(outer.comments)

            def do_POST(self):
                body = self._body()
                outer.calls.append(("POST", self.path, body))
                self._reply({"id": 99})

            def do_PATCH(self):
                body = self._body()
                outer.calls.append(("PATCH", self.path, body))
                self._reply({"id": 5})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gh():
    servers = []

    def make(**kw):
        servers.append(FakeGitHub(**kw))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


def test_upsert_creates_when_no_marker_found(gh):
    api = gh(existing=[{"id": 1, "body": "hello"}])
    result = github.upsert_comment(
        github.sticky_body("hi"), token="t", repository="o/r", pr_number=3, api_url=api.url
    )
    assert result == ("created", 99)
    method, path, body = api.calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/issues/3/comments")
    assert github.marker() in body["body"]


def test_upsert_updates_the_marked_comment_only(gh):
    api = gh(existing=[{"id": 1, "body": "other"}, {"id": 5, "body": github.marker() + "\nold"}])
    result = github.upsert_comment(
        github.sticky_body("new"), token="t", repository="o/r", pr_number=3, api_url=api.url
    )
    assert result == ("updated", 5)
    assert [c[0] for c in api.calls] == ["GET", "PATCH"]
    assert api.calls[-1][1] == "/repos/o/r/issues/comments/5"


def test_upsert_http_error_is_a_comment_error(gh):
    api = gh(status=403)
    with pytest.raises(github.CommentError, match="403"):
        github.upsert_comment("x", token="t", repository="o/r", pr_number=1, api_url=api.url)


def _env(monkeypatch, tmp_path, api_url, *, event="pull_request"):
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"pull_request": {"number": 12}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(ev))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_API_URL", api_url)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))


def test_cli_comment_posts_and_writes_summary(gh, tmp_path, monkeypatch, capsys):
    api = gh()
    _env(monkeypatch, tmp_path, api.url)
    report = tmp_path / "r.json"
    report.write_text(sample_report().to_json())
    assert cli.main(["check", "comment", str(report)]) == 0
    assert "created PR comment" in capsys.readouterr().out
    assert api.calls[-1][2]["body"].startswith(github.marker())
    assert "Redline refactor check" in (tmp_path / "summary.md").read_text()


def test_cli_comment_skips_outside_pull_requests(gh, tmp_path, monkeypatch, capsys):
    api = gh()
    _env(monkeypatch, tmp_path, api.url, event="push")
    md = tmp_path / "r.md"
    md.write_text("### hi\n")
    assert cli.main(["check", "comment", str(md)]) == 0
    assert "not a pull_request" in capsys.readouterr().out
    assert api.calls == [] and (tmp_path / "summary.md").exists()


def test_cli_comment_without_token_and_no_comment_flag(gh, tmp_path, monkeypatch, capsys):
    api = gh()
    _env(monkeypatch, tmp_path, api.url)
    md = tmp_path / "r.md"
    md.write_text("### hi\n")
    assert cli.main(["check", "comment", str(md), "--no-comment"]) == 0
    monkeypatch.delenv("GITHUB_TOKEN")
    assert cli.main(["check", "comment", str(md)]) == 0
    assert api.calls == []


def test_cli_comment_failure_does_not_fail_the_job(gh, tmp_path, monkeypatch, capsys):
    api = gh(status=403)
    _env(monkeypatch, tmp_path, api.url)
    md = tmp_path / "r.md"
    md.write_text("### hi\n")
    assert cli.main(["check", "comment", str(md)]) == 0
    assert "comment skipped" in capsys.readouterr().err


def test_cli_comment_dry_run_and_bad_report(tmp_path, capsys):
    md = tmp_path / "r.md"
    md.write_text("### hi\n")
    assert cli.main(["check", "comment", str(md), "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith(github.marker())
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert cli.main(["check", "comment", str(bad)]) == 2
    assert cli.main(["check", "comment", str(tmp_path / "missing.json")]) == 2


def test_action_yml_is_well_formed():
    yaml = pytest.importorskip("yaml")
    if not ACTION.exists():
        pytest.skip("action.yml is not part of this checkout")
    data = yaml.safe_load(ACTION.read_text())
    assert data["runs"]["using"] == "composite"
    assert data["inputs"]["suite"]["required"] is True
    for name in ("baseline", "base-url", "fail-on", "python-version", "comment", "github-token"):
        assert name in data["inputs"]
    assert all("shell" in s for s in data["runs"]["steps"] if "run" in s)
    commands = "\n".join(s.get("run", "") for s in data["runs"]["steps"])
    assert "redline check run" in commands and "redline check comment" in commands
