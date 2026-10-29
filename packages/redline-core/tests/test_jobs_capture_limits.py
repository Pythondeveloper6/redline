"""Job helper capture remains bounded independently of public presentation tags."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from redline_core import Recorder, Settings, job, row_outcome, stage
from redline_core.jobs import tag


def test_row_counters_do_not_parse_public_tags():
    rec = Recorder()
    with job("import", recorder=rec) as builder:
        builder.tags["job.row_outcomes"] = "invalid JSON supplied by application"
        row_outcome("failed", "one")
        builder.tags["job.row_outcomes"] = '{"arbitrary": "unsafe"}'
        row_outcome("failed", "one")
        assert list(builder.job_outcomes.values()) == [2]
        assert list(json.loads(builder.tags["job.row_outcomes"]).values()) == [2]


def test_many_categories_have_bounded_private_counts_and_valid_clipped_tags():
    rec = Recorder(Settings(max_tag_bytes=32))
    with job("import", recorder=rec) as builder:
        for index in range(500):
            row_outcome("failed", f"reason-{index}")
        assert len(builder.job_outcomes) <= 105
        assert sum(builder.job_outcomes.values()) == 500
        assert json.loads(builder.tags["job.row_outcomes"]) == {"failed:other": 500}
        assert len(json.dumps(builder.tags["job.row_outcomes"])) <= 32
        assert builder.capture_limited


def test_small_tag_budget_many_statuses_remains_valid_json():
    rec = Recorder(Settings(max_tag_bytes=32))
    with job("import", recorder=rec) as builder:
        for status in ("failed", "parked", "conflict", "skipped", "succeeded"):
            row_outcome(status)
        assert sum(builder.job_outcomes.values()) == 5
        assert type(json.loads(builder.tags["job.row_outcomes"])) is dict
        assert builder.capture_limited


def test_job_helpers_do_not_convert_arbitrary_objects():
    class Hostile:
        def __str__(self):
            raise AssertionError("application conversion called")

        __repr__ = __str__

    class HostileParent(dict):
        def get(self, key):
            raise AssertionError("application mapping accessed")

    rec = Recorder()
    with job(Hostile(), recorder=rec, task_id=Hostile(), queue=Hostile(), parent=HostileParent()):
        with stage(Hostile()):
            tag("safe", Hostile())
            tag(Hostile(), "value")
            row_outcome("failed", Hostile(), row_id=Hostile())
    assert rec.last_trace.tags["safe"] == "null"
    assert rec.last_trace.tags["job.outcome"] == "succeeded"
    assert "Hostile" not in json.dumps(rec.last_trace.to_dict())


def test_job_tags_and_stage_labels_are_bounded_and_redacted():
    rec = Recorder(Settings(max_tag_bytes=64))
    with job("import", recorder=rec) as builder:
        tag("detail", "password=private")
        tag("huge", "🙂" * 100_000)
        row_outcome("failed", "x" * 1_000_000)
        with stage("password=private"):
            pass
        assert "private" not in builder.tags["detail"]
        assert len(json.dumps(builder.tags["huge"], ensure_ascii=True)) <= 64
        assert builder.capture_limited
    assert "private" not in rec.last_trace.spans[0].name


def test_row_counters_are_thread_safe_with_copied_context():
    rec = Recorder()
    with job("import", recorder=rec) as builder:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(copy_context().run, row_outcome, "failed", "same") for _ in range(80)
            ]
            for future in futures:
                future.result()
        assert list(builder.job_outcomes.values()) == [80]
        assert list(json.loads(builder.tags["job.row_outcomes"]).values()) == [80]
