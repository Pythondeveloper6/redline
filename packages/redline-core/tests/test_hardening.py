"""Settings coercion, retention semantics, fork safety, the span cap and shared parsing."""

from __future__ import annotations

import os
import pathlib
import sys
import time

import pytest

from redline_core.conf import Settings, SettingsError
from redline_core.recorder import Recorder, record_query
from redline_core.sampling import Sampler, is_error, profile_flag, profile_requested
from redline_core.storage import SQLiteStore

from .conftest import make_trace

# ── settings ──────────────────────────────────────────────────────────────────


def test_pathlike_storage_path_and_project_root(tmp_path):
    s = Settings.from_mapping({"STORAGE_PATH": tmp_path / "r.db", "PROJECT_ROOT": tmp_path})
    assert s.storage_path == str(tmp_path / "r.db") and isinstance(s.storage_path, str)
    assert s.project_root == str(tmp_path)
    direct = Settings(storage_path=pathlib.Path("x.db"))  # type: ignore[arg-type]
    assert direct.storage_path == "x.db"
    assert s.replace(sample_rate=0.5).storage_path == s.storage_path
    with pytest.raises(SettingsError, match="PathLike"):
        Settings.from_mapping({"storage_path": 3})


@pytest.mark.parametrize("name", ["max_rows", "max_age_days", "max_spans_per_trace"])
def test_zero_or_none_disables_limits(name):
    assert getattr(Settings.from_mapping({name: 0}), name) == 0
    assert getattr(Settings.from_mapping({name: None}), name) is None
    with pytest.raises(SettingsError, match="0 or None"):
        Settings.from_mapping({name: -1})
    with pytest.raises(SettingsError):
        Settings.from_mapping({name: "10"})


def test_max_age_s_is_none_when_disabled():
    assert Settings().max_age_s == 7 * 86_400.0
    assert Settings.from_mapping({"max_age_days": 0}).max_age_s is None
    assert Settings.from_mapping({"max_age_days": None}).max_age_s is None
    assert Settings().max_spans_per_trace == 2_000


# ── retention ─────────────────────────────────────────────────────────────────


def _fill(store, n=5, old=1):
    now = time.time()
    for i in range(old):
        store.save(make_trace(started_at=now - 30 * 86_400 - i))
    for i in range(n):
        store.save(make_trace(started_at=now - i))


def test_max_rows_zero_disables_the_row_rule(tmp_path):
    s = SQLiteStore(str(tmp_path / "r.db"), background=False, max_rows=0, max_age_s=0)
    _fill(s)
    assert s.max_rows is None and s.max_age_s is None
    assert s.prune() == 0 and s.count() == 6
    assert s.prune(max_rows=0, max_age_s=0) == 0 and s.count() == 6  # never "delete all"
    assert s.prune(max_age_s=86_400) == 1 and s.count() == 5
    assert s.prune(max_rows=2) == 3 and s.count() == 2
    s.close()


def test_from_settings_disables_rules(tmp_path):
    cfg = Settings.from_mapping(
        {"storage_path": tmp_path / "r.db", "max_rows": None, "max_age_days": 0}
    )
    s = SQLiteStore.from_settings(cfg, background=False)
    _fill(s)
    assert s.prune() == 0 and s.count() == 6
    s.close()


def test_negative_retention_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        SQLiteStore(str(tmp_path / "a.db"), max_rows=-1)
    s = SQLiteStore(str(tmp_path / "b.db"), background=False)
    with pytest.raises(ValueError):
        s.prune(max_rows=-1)
    with pytest.raises(ValueError):
        s.prune(max_age_s=-5)
    s.close()


# ── fork safety ───────────────────────────────────────────────────────────────


def test_writer_starts_lazily(tmp_path):
    s = SQLiteStore(str(tmp_path / "r.db"))
    assert s._thread is None  # nothing running until the first save
    s.save(make_trace())
    assert s._thread is not None and s._thread.is_alive()
    assert s.flush(5) and s.count() == 1
    s.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX only")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # fork() with threads running
def test_store_is_fork_safe_with_a_running_writer(tmp_path):
    """gunicorn --preload: the store (and its writer) exist in the master before fork."""
    s = SQLiteStore(str(tmp_path / "r.db"))
    s.save(make_trace(path="/parent/"))
    assert s.flush(5) and s.count() == 1
    parent_thread = s._thread

    pid = os.fork()
    if pid == 0:  # child
        code = 1
        try:
            assert s._thread is None and s._queue is None  # reset by the fork hook
            for _ in range(3):
                s.save(make_trace(path="/child/"))
            ok = s.flush(5)
            assert s._thread is not None and s._thread.is_alive()
            assert s._thread is not parent_thread
            items = s.list(None, limit=50).items
            code = 0 if ok and sum(i["path"] == "/child/" for i in items) == 3 else 2
            s.close()
        except BaseException:
            import traceback

            traceback.print_exc()
        finally:
            sys.stdout.flush()
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    # the parent still works and sees the child's rows (same file)
    s.save(make_trace(path="/parent/"))
    assert s.flush(5)
    paths = [i["path"] for i in s.list(None, limit=50).items]
    assert paths.count("/child/") == 3 and paths.count("/parent/") == 2
    s.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX only")
def test_store_pid_check_covers_forks_the_hook_missed(tmp_path):
    s = SQLiteStore(str(tmp_path / "r.db"))
    s.save(make_trace())
    assert s.flush(5)
    s._pid = -1  # pretend we are in a child the at-fork hook did not see
    s.save(make_trace())
    assert s._pid == os.getpid() and s.flush(5) and s.count() == 2
    s.close()


# ── span cap ──────────────────────────────────────────────────────────────────


def _recorder(store=None, **cfg):
    return Recorder(
        Settings.from_mapping({"project_root": os.path.dirname(__file__), **cfg}), store
    )


def _loop(n, sql="SELECT * FROM items WHERE order_id = %s", duration=0.001):
    for i in range(n):
        record_query(sql, [i], start=time.perf_counter(), duration=duration, vendor="sqlite")


def test_span_cap_keeps_counts_and_n_plus_one(store):
    rec = _recorder(store, max_spans_per_trace=50, too_many_queries=10_000)
    with rec.recording(method="GET", path="/big/"):
        _loop(500)
    t = rec.last_trace
    assert len(t.spans) == 50
    assert t.query_count == 500 and t.query_ms == pytest.approx(500.0, rel=0.01)
    assert t.truncated == {"dropped_spans": 450, "max_spans": 50}
    (n1,) = [i for i in t.issues if i.kind == "n_plus_one"]
    assert n1.count == 500 and n1.total_ms == pytest.approx(500.0, rel=0.01)
    assert len(n1.span_ids) == 50 and n1.callsite is not None
    assert n1.suggestion is not None
    stored = store.get(t.id)
    assert stored.truncated == t.truncated and stored.query_count == 500


def test_n_plus_one_found_entirely_past_the_cap(store):
    rec = _recorder(store, max_spans_per_trace=5, too_many_queries=10_000)
    with rec.recording(method="GET", path="/big/"):
        _loop(5, "SELECT * FROM a WHERE id = %s")
        _loop(30, "SELECT * FROM b WHERE id = %s", duration=0.2)
    t = rec.last_trace
    kinds = {(i.kind, i.count) for i in t.issues}
    assert ("n_plus_one", 5) in kinds and ("n_plus_one", 30) in kinds
    assert ("slow_query", 30) in kinds
    (late,) = [i for i in t.issues if i.kind == "n_plus_one" and i.count == 30]
    assert late.span_ids == [] and late.callsite is not None and late.suggestion is not None


def test_span_cap_disabled_and_structural_spans_kept(store):
    from redline_core import span

    rec = _recorder(store, max_spans_per_trace=0, too_many_queries=10_000)
    with rec.recording(method="GET", path="/big/"):
        _loop(300)
    assert len(rec.last_trace.spans) == 300 and rec.last_trace.truncated is None

    rec = _recorder(store, max_spans_per_trace=3)
    with rec.recording(method="GET", path="/x/"):
        with span("view", kind="view"):
            _loop(10)
    kinds = [s.kind for s in rec.last_trace.spans]
    assert kinds.count("view") == 1 and kinds.count("sql") == 3


# ── shared parsing / error semantics ─────────────────────────────────────────


@pytest.mark.parametrize("value", ["profile", "1", "true", "YES", " on ", b"1"])
def test_profile_flag_values(value):
    assert profile_flag(value)
    assert profile_requested(header=value)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("_redline=profile", True),
        ("a=1&_redline=1", True),
        (b"_redline=true", True),
        ("_redline=0", False),
        ("_redline=", False),
        ("x_redline=1", False),
        ("", False),
        (None, False),
        ({"_redline": "yes"}, True),
        ({"_redline": ["no", "on"]}, True),
        ({"_redline": ["off"]}, False),
        ({"other": "1"}, False),
    ],
)
def test_profile_requested_query(query, expected):
    assert profile_requested(query) is expected


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (500, False, True),
        (503, False, True),
        (500, True, True),
        (404, True, False),  # Django's Http404 is raised, but it is a client error
        (403, True, False),
        (404, False, False),
        (200, True, True),  # failed after a 200 was sent (streaming)
        (200, False, False),
    ],
)
def test_is_error(status, error, expected):
    assert is_error(status, error) is expected


def test_sampler_never_keeps_4xx_as_error():
    s = Sampler(
        Settings.from_mapping({"sample_rate": 0.0, "always_keep_slower_than_ms": 1e9}),
        rng=lambda: 1.0,
    )
    assert s.keep(rolled=False, status=404, duration_ms=1, error=True) is None
    assert s.keep(rolled=False, status=500, duration_ms=1, error=True) == "error"
    assert s.keep(rolled=False, status=502, duration_ms=1) == "error"
