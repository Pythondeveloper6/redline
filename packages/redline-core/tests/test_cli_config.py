"""``redline config``, ``redline doctor`` and ``redline hash-password``."""

from __future__ import annotations

import io
import json
import sys

import pytest

from redline_core import cli
from redline_core.access import verify_password

TOKEN = "t" * 32


@pytest.fixture
def project(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("REDLINE_") and not key.startswith("REDLINE_TEST_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = cli.main(list(args))
    finally:
        sys.stdout, sys.stderr = old
    return code, out.getvalue(), err.getvalue()


def test_config_text_and_json(project, monkeypatch):
    (project / "redline.toml").write_text(
        f'access = "token"\ntoken = "{TOKEN}"\nslow_query_ms = 12\n'
    )
    monkeypatch.setenv("REDLINE_SAMPLE_RATE", "0.5")
    code, out, _ = run("config")
    assert code == 0
    assert "slow_query_ms" in out and "(file)" in out
    assert "env REDLINE_SAMPLE_RATE" in out
    assert TOKEN not in out and '"***"' not in out and "***" in out
    assert "secret_key" in out  # the warning
    code, out, _ = run("config", "--json")
    data = json.loads(out)
    assert data["sources"]["sample_rate"] == "env" and data["settings"]["token"] == "***"
    assert data["file"].endswith("redline.toml")


def test_config_project_root_option(project, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    (other / "redline.toml").write_text("slow_query_ms = 3\n")
    code, out, _ = run("config", "--json", "--project-root", str(other))
    assert code == 0 and json.loads(out)["settings"]["slow_query_ms"] == 3


def test_config_invalid(project, monkeypatch):
    monkeypatch.setenv("REDLINE_SAMPEL_RATE", "1")
    code, _, err = run("config")
    assert code == 2 and "did you mean REDLINE_SAMPLE_RATE" in err
    code, out, _ = run("doctor")
    assert code == 1 and "FAIL" in out


def test_doctor_ok_in_dev(project):
    code, out, _ = run("doctor")
    assert "storage" in out and "access" in out and "retention" in out
    # the UI bundle may be missing in a source checkout; everything else passes
    assert code == (0 if "FAIL  ui bundle" not in out else 1)


def test_doctor_flags_risky_settings(project, monkeypatch):
    (project / "redline.toml").write_text(
        'preset = "production"\n'
        f'token = "{TOKEN}"\n'
        "capture_request_body = true\n"
        "history_retention_days = 0\n"
        "load_from_ui = true\n"
    )
    _, out, _ = run("doctor", "--json")
    checks = {(c["check"], c["status"]) for c in json.loads(out)}
    assert ("capture", "warn") in checks
    assert ("retention", "warn") in checks
    assert ("sessions", "warn") in checks
    assert ("proxies", "warn") in checks
    assert ("token", "ok") in checks


def test_doctor_open_and_local_and_basic(project, monkeypatch):
    (project / "redline.toml").write_text('access = "open"\n')
    code, out, _ = run("doctor")
    assert code == 1 and "FAIL  access" in out
    monkeypatch.setenv("REDLINE_ALLOW_OPEN", "1")
    code, out, _ = run("doctor")
    assert "WARN  access" in out
    monkeypatch.delenv("REDLINE_ALLOW_OPEN")
    (project / "redline.toml").write_text('preset = "staging"\naccess = "local"\n')
    assert "WARN  access" in run("doctor")[1]
    weak = "pbkdf2_sha256$1000$YQ$Yg"
    (project / "redline.toml").write_text(f'access = "basic"\n[basic_users]\nann = "{weak}"\n')
    assert "weak hash" in run("doctor")[1]


def test_doctor_token_file_permissions(project):
    f = project / "token"
    f.write_text(TOKEN)
    f.chmod(0o644)
    (project / "redline.toml").write_text(f'access = "token"\ntoken_file = "{f}"\n')
    out = run("doctor")[1]
    if __import__("os").name == "posix":
        assert "readable by other users" in out
    f.write_text("short")
    assert "only 5 characters" in run("doctor")[1]


def test_doctor_unwritable_storage(project):
    (project / "redline.toml").write_text('storage_path = "/proc/definitely/not/here.db"\n')
    code, out, _ = run("doctor")
    assert code == 1 and "FAIL  storage" in out


class _Stdin(io.StringIO):
    def __init__(self, text: str, tty: bool) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_hash_password_interactive(monkeypatch):
    answers = iter(["hunter2", "hunter2"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(sys, "stdin", _Stdin("", tty=True))
    code, out, _ = run("hash-password", "--iterations", "1000")
    assert code == 0 and verify_password("hunter2", out.strip())


def test_hash_password_mismatch_and_empty(monkeypatch):
    answers = iter(["a", "b"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(sys, "stdin", _Stdin("", tty=True))
    code, _, err = run("hash-password")
    assert code == 2 and "do not match" in err
    monkeypatch.setattr(sys, "stdin", _Stdin("\n", tty=False))
    assert run("hash-password")[0] == 2
    monkeypatch.setattr(sys, "stdin", _Stdin("pw\n", tty=False))
    assert run("hash-password", "--iterations", "10")[0] == 2


def test_hash_password_from_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin("s3cret\n", tty=False))
    code, out, _ = run("hash-password", "--iterations", "1000")
    assert code == 0 and out.startswith("pbkdf2_sha256$1000$")
    assert verify_password("s3cret", out.strip())
