from __future__ import annotations

import json
import time

import pytest

from redline_core import cli, diagnose
from redline_core.conf import Settings, SettingsError
from redline_core.detect import detect
from redline_core.storage.sqlite import SQLiteStore

from ..conftest import make_callsite, make_trace, sql_span

KEY = "sk-ant-test-" + "k" * 30


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


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        return 200, json.dumps(
            {"content": [{"type": "text", "text": "Use select_related."}]}
        ).encode()

    monkeypatch.setattr(diagnose, "_send", fake)
    return calls


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("REDLINE_AI_API_KEY", "REDLINE_AI_DIAGNOSIS"):
        monkeypatch.delenv(name, raising=False)


GROUP = {
    "id": "i1",
    "kind": "n_plus_one",
    "title": "N+1 token=sk-ant-" + "a" * 40,
    "detail": "d",
    "fingerprint": "SELECT ? FROM t",
    "routes": ["/o"],
    "occurrences": 3,
    "callsite": {"file": "shop/v.py", "line": 4, "function": "f", "code": "x = 1", "stack": [1]},
    "suggestion": {"summary": "s", "diff": "SECRET-DIFF"},
    "why": {"rule": "r", "observed": "o", "threshold": "5", "limits": "l", "grouping": "g"},
    "secret_extra": "must-not-leak",
}


def test_payload_is_an_allowlist_and_redacted():
    payload = diagnose.build_payload(GROUP)
    blob = json.dumps(payload)
    assert "must-not-leak" not in blob and "SECRET-DIFF" not in blob
    assert "sk-ant-" + "a" * 40 not in blob
    assert payload["callsite"] == {"file": "shop/v.py", "line": 4, "function": "f", "code": "x = 1"}
    assert payload["kind"] == "n_plus_one" and payload["why"]["threshold"] == "5"


def test_preview_shows_destination_and_exact_body():
    text = diagnose.preview(diagnose.build_payload(GROUP), "anthropic", "claude-x")
    assert "api.anthropic.com" in text and "claude-x" in text and "n_plus_one" in text
    assert "Nothing has been sent" in text


def test_diagnose_posts_fixed_host_with_key_header(sent):
    out = diagnose.diagnose(diagnose.build_payload(GROUP), "anthropic", "claude-x", KEY)
    assert out == "Use select_related."
    call = sent[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == KEY
    assert call["body"]["model"] == "claude-x" and call["body"]["max_tokens"] <= 1500
    assert "n_plus_one" in json.dumps(call["body"]["messages"])


@pytest.mark.parametrize(
    "status,body",
    [
        (401, b'{"error":{"message":"bad key ' + KEY.encode() + b'"}}'),
        (500, b"oops"),
        (200, b"not json"),
        (200, b'{"content": []}'),
    ],
)
def test_failures_raise_without_leaking_key_or_body(monkeypatch, status, body):
    monkeypatch.setattr(diagnose, "_send", lambda *a, **k: (status, body))
    with pytest.raises(diagnose.DiagnoseError) as exc:
        diagnose.diagnose({"kind": "x"}, "anthropic", "m", KEY)
    assert KEY not in str(exc.value) and "oops" not in str(exc.value)


def test_unknown_provider_refused():
    with pytest.raises(diagnose.DiagnoseError):
        diagnose.diagnose({}, "evil", "m", KEY)


def test_settings_validation():
    s = Settings()
    assert s.ai_diagnosis is False and s.ai_provider == "anthropic"
    with pytest.raises(SettingsError):
        Settings.from_mapping({"ai_provider": "evil"})
    with pytest.raises(SettingsError):
        Settings.from_mapping({"ai_diagnosis": "yes"})
    with pytest.raises(SettingsError):
        Settings.from_mapping({"ai_model": ""})


def test_cli_default_only_previews_and_never_sends(store_path, sent, capsys):
    path, iid = store_path
    assert cli.main(["diagnose", iid, "--store", path]) == 0
    out = capsys.readouterr().out
    assert "Nothing has been sent" in out and sent == []


def test_cli_send_refused_unless_enabled_and_keyed(store_path, sent, monkeypatch, capsys):
    path, iid = store_path
    assert cli.main(["diagnose", iid, "--store", path, "--send"]) == 2  # not enabled
    assert "ai_diagnosis" in capsys.readouterr().err
    monkeypatch.setenv("REDLINE_AI_DIAGNOSIS", "true")
    assert cli.main(["diagnose", iid, "--store", path, "--send"]) == 2  # no key
    assert "REDLINE_AI_API_KEY" in capsys.readouterr().err
    assert sent == []


def test_cli_send_when_opted_in(store_path, sent, monkeypatch, capsys):
    path, iid = store_path
    monkeypatch.setenv("REDLINE_AI_DIAGNOSIS", "true")
    monkeypatch.setenv("REDLINE_AI_API_KEY", KEY)
    assert cli.main(["diagnose", iid, "--store", path, "--send"]) == 0
    captured = capsys.readouterr()
    assert "Use select_related." in captured.out and KEY not in captured.out + captured.err
    assert len(sent) == 1


def test_cli_error_paths(store_path, sent, monkeypatch, capsys):
    path, iid = store_path
    assert cli.main(["diagnose", "nope", "--store", path]) == 2
    assert cli.main(["diagnose", iid, "--store", path + ".missing"]) == 2
    monkeypatch.setenv("REDLINE_AI_DIAGNOSIS", "true")
    monkeypatch.setenv("REDLINE_AI_API_KEY", KEY)
    monkeypatch.setattr(diagnose, "_send", lambda *a, **k: (500, b"x"))
    assert cli.main(["diagnose", iid, "--store", path, "--send"]) == 1
    assert KEY not in capsys.readouterr().err


def test_api_key_env_is_not_a_setting_and_never_shown(monkeypatch):
    from redline_core.config import resolve_settings

    monkeypatch.setenv("REDLINE_AI_API_KEY", KEY)
    resolved = resolve_settings(None, read_file=False)
    assert KEY not in json.dumps(resolved.settings.public_dict(), default=str)
    assert not hasattr(resolved.settings, "ai_api_key")
