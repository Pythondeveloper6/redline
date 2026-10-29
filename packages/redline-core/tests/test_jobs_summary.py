from __future__ import annotations

import time

import pytest

from redline_core import Recorder, Settings
from redline_core.api import RedlineAPI
from redline_core.jobs import stage
from redline_core.model import Trace
from redline_core.storage.sqlite import SQLiteStore


def _task(store, trace_id, name, outcome, *, stage_name=None, duration=10.0):
    tags = {
        "job.name": name,
        "job.outcome": outcome,
        "job.queue": "default",
        "job.attempt": "0",
    }
    if stage_name:
        tags["job.first_failing_stage"] = stage_name
    store.save(
        Trace(
            id=trace_id,
            framework="other",
            method="TASK",
            path=name,
            route=name,
            status=200,
            started_at=time.time(),
            duration_ms=duration,
            tags=tags,
        )
    )


def test_stage_records_outcome_and_first_failure():
    recorder = Recorder(Settings(sample_rate=0))
    with recorder.recording(method="TASK", path="sync.orders", route="sync.orders"):
        with pytest.raises(ValueError):
            with stage("extract"):
                raise ValueError("bad row")
        with stage("load"):
            pass
    trace = recorder.last_trace
    assert trace.tags["job.first_failing_stage"] == "extract"
    by_name = {span.name: span for span in trace.spans if span.data.get("job_stage")}
    assert by_name["extract"].data["stage_outcome"] == "failed"
    assert by_name["load"].data["stage_outcome"] == "succeeded"


def test_first_failing_stage_does_not_overwrite_the_earliest_failure():
    recorder = Recorder(Settings(sample_rate=0))
    with recorder.recording(method="TASK", path="sync.orders", route="sync.orders"):
        for name in ("extract", "transform"):
            with pytest.raises(RuntimeError):
                with stage(name):
                    raise RuntimeError(name)
    assert recorder.last_trace.tags["job.first_failing_stage"] == "extract"


def test_job_summary_distributes_outcomes_and_stages(tmp_path):
    store = SQLiteStore(str(tmp_path / "redline.db"), background=False)
    try:
        _task(store, "a", "sync.orders", "failed", stage_name="extract", duration=30)
        _task(store, "b", "sync.orders", "failed", stage_name="extract", duration=50)
        _task(store, "c", "sync.orders", "succeeded", duration=10)
        _task(store, "d", "sync.users", "failed", duration=20)
        api = RedlineAPI(store, Settings())
        status, payload = api.dispatch(
            "GET", "api/jobs/summary", params={"window": "1h", "limit": "100"}
        )
        assert status == 200
        assert payload["sampled"] == 4 and payload["total"] == 4
        assert payload["outcomes"] == {"failed": 3, "succeeded": 1}
        assert payload["first_failing_stages"] == {"extract": 2}
        assert payload["unattributed_failures"] == 1
        orders = next(job for job in payload["jobs"] if job["name"] == "sync.orders")
        assert orders["total"] == 3
        assert orders["outcomes"] == {"failed": 2, "succeeded": 1}
        assert orders["first_failing_stages"] == {"extract": 2}
        assert orders["p50_ms"] == 30.0
        assert orders["p95_ms"] == 48.0
    finally:
        store.close()
