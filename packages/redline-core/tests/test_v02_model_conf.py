from __future__ import annotations

import subprocess

import pytest

from redline_core import conf
from redline_core.api import RedlineAPI
from redline_core.conf import Settings, SettingsError, editor_url_template, resolve_release
from redline_core.detect import Detector
from redline_core.detectors import REGISTRY
from redline_core.model import ISSUE_KINDS, SPAN_KINDS, Callsite, Issue, LogRecord, Span, Trace
from redline_core.storage import SQLiteStore

# ── model ──────────────────────────────────────────────────────────────────────


def test_new_kinds_present():
    assert {"serializer", "resolver", "transaction", "blocking", "pool_wait"} <= set(SPAN_KINDS)
    assert {"blocking_in_async", "retry_storm", "large_result"} <= set(ISSUE_KINDS)


def test_log_record_roundtrip():
    rec = LogRecord(1.5, "WARNING", "shop", "hello", Callsite("a.py", 3, "f", "x = 1"))
    back = LogRecord.from_dict(rec.to_dict())
    assert back == rec
    assert LogRecord.from_dict({"message": "m"}).level == "INFO"


def test_trace_roundtrip_with_v02_fields():
    t = Trace(id="a" * 32, framework="other", method="GET", path="/")
    t.logs = [LogRecord(0.1, "INFO", "x", "m")]
    t.logs_dropped = 4
    t.release = "abc123"
    t.issues = [Issue(id="i", kind="retry_storm", severity="warning", title="t", detail="d")]
    t.issues[0].extra = {"host": "api.example.com"}
    back = Trace.from_dict(t.to_dict())
    assert back.logs == t.logs and back.logs_dropped == 4 and back.release == "abc123"
    assert back.issues[0].extra == {"host": "api.example.com"}
    assert back.issues[0].kind == "retry_storm"


def test_v01_rows_still_load():
    old = {
        "id": "b" * 32,
        "framework": "django",
        "method": "GET",
        "path": "/x",
        "spans": [{"id": "s", "kind": "sql", "name": "q"}],
        "issues": [{"id": "i", "kind": "n_plus_one", "severity": "warning"}],
    }
    t = Trace.from_dict(old)
    assert t.logs == [] and t.logs_dropped == 0 and t.release is None
    assert t.issues[0].extra == {}
    assert t.summary()["id"] == "b" * 32
    assert isinstance(t.spans[0], Span)


def test_summary_keys_unchanged():
    keys = Trace(id="a", framework="other", method="GET", path="/").summary().keys()
    assert set(keys) == {
        "id", "method", "path", "route", "view", "status", "started_at", "duration_ms",
        "query_count", "query_ms", "issue_count", "issue_kinds", "has_profile", "tags",
    }  # fmt: skip


def test_store_roundtrip_keeps_logs(store):
    t = Trace(id="c" * 32, framework="other", method="GET", path="/", release="r1")
    t.logs = [LogRecord(0.2, "ERROR", "x", "boom")]
    store.save(t)
    store.flush(2)
    got = store.get(t.id)
    assert got is not None and got.release == "r1" and got.logs[0].message == "boom"


# ── settings ───────────────────────────────────────────────────────────────────


def test_defaults():
    s = Settings()
    assert (s.blocking_ms, s.detect_blocking) == (20, True)
    assert (s.large_result_rows, s.unbounded_rows) == (1000, 200)
    assert (s.slow_serializer_ms, s.long_transaction_ms, s.slow_external_ms) == (50, 200, 500)
    assert (s.capture_logs, s.log_level, s.max_logs_per_trace) == (True, "INFO", 200)
    assert (s.capture_locals, s.editor, s.server_timing) == (True, "vscode", "auto")
    assert s.badge == "auto" and s.release is None
    assert (s.history_retention_days, s.load_from_ui) == (90, False)


def test_log_level_normalised():
    assert Settings.from_mapping({"log_level": "warning"}).log_level == "WARNING"
    assert Settings(log_level="error").log_level == "ERROR"
    assert Settings.from_mapping({"LOG_LEVEL": " debug "}).log_level == "DEBUG"
    assert Settings().replace(log_level="critical").log_level == "CRITICAL"
    for bad in ("verbose", "", 10):
        with pytest.raises(SettingsError):
            Settings.from_mapping({"log_level": bad})


@pytest.mark.parametrize("value", ["vscode", "cursor", "pycharm", "idea", "sublime", "none"])
def test_known_editors(value):
    assert Settings.from_mapping({"editor": value}).editor == value


def test_editor_template_validation():
    ok = "myed://open?f={file}&l={line}"
    assert Settings.from_mapping({"editor": ok}).editor == ok
    for bad in ("emacs", "myed://{file}", "myed://{line}", 3):
        with pytest.raises(SettingsError):
            Settings.from_mapping({"editor": bad})


def test_editor_url_templates():
    def t(editor):
        return editor_url_template(Settings(editor=editor))

    assert t("vscode") == "vscode://file/{file}:{line}"
    assert t("cursor") == "cursor://file/{file}:{line}"
    assert t("pycharm") == "pycharm://open?file={file}&line={line}"
    assert t("idea") == "idea://open?file={file}&line={line}"
    assert t("sublime") == "subl://open?url=file://{file}&line={line}"
    assert t("none") is None
    assert t("x://{file}#{line}") == "x://{file}#{line}"


def test_badge_validation():
    assert Settings.from_mapping({"badge": True}).badge is True
    assert Settings.from_mapping({"badge": False}).badge is False
    assert Settings.from_mapping({"badge": "AUTO"}).badge == "auto"
    for bad in ("yes", 1, None):
        with pytest.raises(SettingsError):
            Settings.from_mapping({"badge": bad})


@pytest.mark.parametrize(
    "key,bad",
    [
        ("blocking_ms", -1),
        ("blocking_ms", "x"),
        ("large_result_rows", -1),
        ("unbounded_rows", 1.5),
        ("max_logs_per_trace", -3),
        ("capture_logs", "yes"),
        ("capture_locals", 1),
        ("detect_blocking", None),
        ("server_timing", "on"),
        ("load_from_ui", 1),
        ("history_retention_days", -1),
        ("release", 5),
        ("slow_external_ms", True),
    ],
)
def test_invalid_values(key, bad):
    with pytest.raises(SettingsError):
        Settings.from_mapping({key: bad})


def test_optional_and_zero_values_ok():
    s = Settings.from_mapping({"history_retention_days": None, "max_logs_per_trace": 0})
    assert s.history_retention_days is None and s.max_logs_per_trace == 0
    assert Settings.from_mapping({"release": "v1.2"}).release == "v1.2"


def test_to_dict_roundtrip():
    s = Settings(log_level="debug", editor="idea", badge=False)
    assert Settings.from_mapping(s.to_dict()) == s


# ── release ────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_release(monkeypatch):
    for name in conf._RELEASE_ENV:
        monkeypatch.delenv(name, raising=False)
    conf._release_cache.clear()
    yield
    conf._release_cache.clear()


def test_release_explicit_wins(clean_release, monkeypatch):
    monkeypatch.setenv("REDLINE_RELEASE", "env")
    assert resolve_release(Settings(release="explicit")) == "explicit"


def test_release_env_order(clean_release, monkeypatch):
    monkeypatch.setenv("HEROKU_SLUG_COMMIT", "heroku")
    assert resolve_release(Settings()) == "heroku"
    monkeypatch.setenv("GIT_SHA", "gitsha")
    assert resolve_release(Settings()) == "gitsha"
    monkeypatch.setenv("REDLINE_RELEASE", "rl")
    assert resolve_release(Settings()) == "rl"


def test_release_from_git_cached(clean_release, tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["cwd"], kw["timeout"]))
        return subprocess.CompletedProcess(cmd, 0, stdout="abc1234\n", stderr="")

    monkeypatch.setattr(conf.subprocess, "run", fake_run)
    s = Settings(project_root=str(tmp_path))
    assert resolve_release(s) == "abc1234"
    assert resolve_release(s) == "abc1234"
    assert len(calls) == 1
    assert calls[0][0][:2] == ["git", "rev-parse"] and calls[0][2] == 1.0


@pytest.mark.parametrize("failure", ["missing", "timeout", "nonzero"])
def test_release_never_raises(clean_release, tmp_path, monkeypatch, failure):
    def fake_run(cmd, **kw):
        if failure == "missing":
            raise FileNotFoundError("git")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="not a repo")

    monkeypatch.setattr(conf.subprocess, "run", fake_run)
    assert resolve_release(Settings(project_root=str(tmp_path))) is None


# ── detector registry ──────────────────────────────────────────────────────────


@pytest.fixture
def registry():
    saved = list(REGISTRY)
    REGISTRY.clear()
    yield REGISTRY
    REGISTRY[:] = saved


def _issue(kind="slow_external"):
    return Issue(id="x" + kind, kind=kind, severity="warning", title="t", detail="d")


def test_registry_runs_and_gets_suggestions(registry):
    seen = {}

    def det(trace, settings):
        seen["args"] = (trace, settings)
        return [_issue("slow_query")]

    registry.append(det)
    settings = Settings(slow_query_ms=7)
    trace = Trace(id="d" * 32, framework="other", method="GET", path="/")
    issues = Detector(settings).detect(trace)
    assert seen["args"] == (trace, settings)
    assert [i.kind for i in issues] == ["slow_query"]
    assert issues[0].suggestion is not None  # suggesters ran over registry issues


def test_registry_exception_is_skipped(registry, caplog):
    def bad(trace, settings):
        raise RuntimeError("boom")

    registry.extend([bad, lambda t, s: [_issue("retry_storm")]])
    trace = Trace(id="d" * 32, framework="other", method="GET", path="/")
    issues = Detector().detect(trace)
    assert [i.kind for i in issues] == ["retry_storm"]
    assert "boom" in caplog.text


def test_new_issue_kinds_accepted_by_api(store):
    api = RedlineAPI(store, Settings(), framework="other")
    status, _ = api.dispatch("GET", "api/issues", params={"kind": "retry_storm"})
    assert status == 200


# ── api meta ───────────────────────────────────────────────────────────────────


def test_meta_additions(tmp_path):
    store = SQLiteStore(":memory:", background=False)
    try:
        api = RedlineAPI(
            store,
            Settings(project_root=str(tmp_path), editor="cursor", release="r9", load_from_ui=True),
            framework="other",
        )
        status, meta = api.dispatch("GET", "api/meta")
        assert status == 200
        assert meta["project_root"] == str(tmp_path)
        assert meta["editor"] == "cursor"
        assert meta["editor_url_template"] == "cursor://file/{file}:{line}"
        assert meta["release"] == "r9"
        assert {"load_from_ui": True, "sse": True, "snapshot": True}.items() <= meta[
            "features"
        ].items()
        assert "thresholds" in meta["settings"]  # v0.1 keys untouched
        _, none_meta = RedlineAPI(store, Settings(editor="none"), framework="other").dispatch(
            "GET", "api/meta"
        )
        assert none_meta["editor_url_template"] is None
    finally:
        store.close()
