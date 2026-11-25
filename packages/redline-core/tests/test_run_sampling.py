"""A registered load run keeps its tagged requests even when sampling would drop them."""

from __future__ import annotations

import time

import pytest

from redline_core.conf import Settings
from redline_core.recorder import Recorder
from redline_core.runs import Run
from redline_core.sampling import Sampler
from redline_core.storage import SQLiteStore


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(str(tmp_path / "r.db"), background=False)
    yield s
    s.close()


def _call(rec, header, path="/orders"):
    with rec.recording(method="GET", path=path, headers=header):
        pass
    return rec.last_trace


def test_sampler_keeps_a_run_call_provisionally():
    s = Sampler(Settings(sample_rate=0.0, always_keep_slower_than_ms=1e9))
    assert s.keep(rolled=False, status=200, duration_ms=1.0) is None
    assert s.keep(rolled=False, status=200, duration_ms=1.0, run=True) == "run"
    assert s.keep(rolled=True, status=200, duration_ms=1.0, run=True) == "rate"


def test_low_sample_rate_still_keeps_traces_of_a_running_run(store):
    rec = Recorder(Settings(sample_rate=0.0, always_keep_slower_than_ms=1e9), store)
    store.save_run(Run(id="live", name="x", status="running", created_at=time.time()))
    plain = _call(rec, {})
    assert store.get(plain.id) is None  # sampled out
    tagged = _call(rec, {"X-Redline-Run": "live;step=1"})
    stored = store.get(tagged.id)
    assert stored is not None
    assert stored.tags["run_id"] == "live" and stored.tags["sampled_by"] == "run"


def test_a_made_up_run_id_forces_nothing_into_the_store(store):
    rec = Recorder(Settings(sample_rate=0.0, always_keep_slower_than_ms=1e9), store)
    forged = _call(rec, {"X-Redline-Run": "made-up;step=1"})
    assert forged.tags["sampled_by"] == "run"  # provisionally...
    assert store.get(forged.id) is None  # ...dropped by the writer


def test_a_finished_run_no_longer_keeps_traces(store):
    rec = Recorder(Settings(sample_rate=0.0, always_keep_slower_than_ms=1e9), store)
    store.save_run(Run(id="old", name="x", status="running", created_at=time.time()))
    store.finish_run("old", "done", {})
    store._conn.execute("UPDATE runs SET finished_at = ?", (time.time() - 120,))
    store._runs_cache = None
    late = _call(rec, {"X-Redline-Run": "old;step=1"})
    assert store.get(late.id) is None


def test_sampled_in_traces_of_unknown_runs_are_stored_without_the_run_tags(store):
    rec = Recorder(Settings(), store)
    t = _call(rec, {"X-Redline-Run": "nope;step=2"})
    stored = store.get(t.id)
    assert stored is not None and "run_id" not in stored.tags and "step" not in stored.tags
