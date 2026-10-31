from __future__ import annotations

import io
import json
import os
import sys

import pytest

from redline_core import cli
from redline_core.config import resolve_settings
from redline_core.production_checks import production_checks

TOKEN = "t" * 32
SIGNING_SECRET = "production-signing-secret-do-not-print-32bytes"


def _resolved(tmp_path, **overrides):
    values = {
        "preset": "production",
        "storage_path": str(tmp_path / "private" / "redline.db"),
        "token": TOKEN,
        "secret_key": SIGNING_SECRET,
        "cookie_secure": True,
        **overrides,
    }
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700, exist_ok=True)
    private_dir.chmod(0o700)
    return resolve_settings(values, environ={}, project_root=tmp_path, read_file=False)


def _statuses(checks):
    return {(status, name) for status, name, _ in checks}


def test_safe_production_profile_passes_policy(tmp_path):
    checks = production_checks(_resolved(tmp_path))
    assert not [item for item in checks if item[0] == "fail"]
    assert not [item for item in checks if item[0] == "warn"]


@pytest.mark.parametrize(
    ("overrides", "check"),
    [
        ({"preset": "staging"}, "preset"),
        ({"capture_request_body": True}, "capture_request_body"),
        ({"capture_response_body": True}, "capture_response_body"),
        ({"capture_sql_params": True}, "capture_sql_params"),
        ({"capture_locals": True}, "capture_locals"),
        ({"capture_logs": True}, "capture_logs"),
        ({"capture_error_details": True}, "capture_error_details"),
        ({"access": "local"}, "access"),
        ({"access": "open"}, "access"),
        ({"read_only": False}, "read only"),
        ({"cookie_secure": "auto"}, "secure cookies"),
        ({"secret_key": "short-signing-secret20"}, "session signing"),
        ({"trusted_proxies": ["0.0.0.0/0"]}, "trusted proxies"),
        ({"trusted_proxies": ["::/0"]}, "trusted proxies"),
        ({"features": {"explain": True}}, "feature explain"),
        ({"profile": "opt_in"}, "profile"),
        ({"badge": True}, "badge"),
        ({"server_timing": True}, "server timing"),
        ({"load_from_ui": True}, "load_from_ui"),
        ({"checks_from_ui": "auto"}, "checks_from_ui"),
        ({"analysis_from_ui": True}, "analysis_from_ui"),
        ({"storage_path": ":memory:"}, "storage privacy"),
        ({"max_rows": None, "max_age_days": None}, "retention"),
    ],
)
def test_unsafe_production_settings_are_explicit_failures(tmp_path, overrides, check):
    checks = production_checks(_resolved(tmp_path, **overrides))
    assert any(status == "fail" and name == check for status, name, _ in checks)


def test_disabled_recording_and_full_sample_rate_are_warnings(tmp_path):
    checks = production_checks(_resolved(tmp_path, enabled=False, sample_rate=1))
    assert ("warn", "enabled") in _statuses(checks)
    assert ("warn", "sampling") in _statuses(checks)
    assert not any(status == "fail" for status, _, _ in checks)


@pytest.mark.parametrize("access", ["framework", "custom"])
def test_application_access_hooks_are_reported_as_unvalidated_warnings(tmp_path, access):
    options = {"access": access}
    if access == "custom":
        options["access_check"] = "app.auth.check"
    checks = production_checks(_resolved(tmp_path, **options))
    assert ("warn", "access hook") in _statuses(checks)


def test_missing_signing_secret_is_a_policy_failure(tmp_path):
    checks = production_checks(_resolved(tmp_path, secret_key=None))
    assert ("fail", "session signing") in _statuses(checks)


def test_production_checks_never_emit_secret_values(tmp_path):
    checks = production_checks(_resolved(tmp_path))
    assert SIGNING_SECRET not in repr(checks)
    assert TOKEN not in repr(checks)


@pytest.fixture
def project(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("REDLINE_") and not key.startswith("REDLINE_TEST_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(*args):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = cli.main(list(args))
    finally:
        sys.stdout, sys.stderr = old
    return code, out.getvalue(), err.getvalue()


def test_default_doctor_json_keeps_existing_check_set(project, monkeypatch):
    monkeypatch.setattr(cli, "_doctor_checks", lambda resolved, **_: [("ok", "existing", "unchanged")])
    code, output, _ = _run("doctor", "--json")
    assert code == 0
    assert json.loads(output) == [{"status": "ok", "check": "existing", "detail": "unchanged"}]


def test_production_json_and_strict_warning_exit(project, monkeypatch):
    private_dir = project / "private"
    private_dir.mkdir(mode=0o700)
    private_dir.chmod(0o700)
    (project / "redline.toml").write_text(
        'preset = "production"\n'
        "cookie_secure = true\n"
        f'storage_path = "{private_dir / "redline.db"}"\n'
        f'token = "{TOKEN}"\n'
        f'secret_key = "{SIGNING_SECRET}"\n'
    )
    monkeypatch.setattr(cli, "_doctor_checks", lambda resolved, **_: [("warn", "probe", "needs review")])

    code, output, _ = _run("doctor", "--production", "--json")
    checks = json.loads(output)
    assert code == 0
    assert checks[0]["status"] == "warn"
    assert any(item["check"] == "preset" for item in checks)
    assert TOKEN not in output and SIGNING_SECRET not in output

    code, output, _ = _run("doctor", "--production", "--strict", "--json")
    checks = json.loads(output)
    assert code == 1
    assert next(item for item in checks if item["check"] == "probe")["status"] == "warn"


def test_doctor_json_config_failure_is_valid_and_secret_free(project, monkeypatch):
    from redline_core import config
    from redline_core.conf import SettingsError

    def invalid(*args, **kwargs):
        raise SettingsError(f"bad setting includes {SIGNING_SECRET}")

    monkeypatch.setattr(config, "resolve_settings", invalid)
    code, output, error = _run("doctor", "--json")
    assert code == 1
    assert json.loads(output) == [
        {"status": "fail", "check": "config", "detail": "configuration does not load"}
    ]
    assert SIGNING_SECRET not in output + error
