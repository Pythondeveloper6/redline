from __future__ import annotations

import json

from redline_core.model import Issue, Suggestion, Trace

from .conftest import make_callsite, make_trace, sql_span


def test_trace_json_round_trip():
    cs = make_callsite()
    trace = make_trace(
        spans=[sql_span("SELECT * FROM t WHERE id = %s", [1], callsite=cs)],
        tags={"run_id": "r1"},
        profile={"shared": {"frames": []}},
    )
    trace.issues.append(
        Issue(
            id="abc",
            kind="n_plus_one",
            severity="warning",
            title="t",
            detail="d",
            span_ids=[trace.spans[0].id],
            fingerprint="fp",
            callsite=cs,
            count=5,
            total_ms=3.5,
            suggestion=Suggestion(summary="s", code="c", diff=None, confidence="high"),
        )
    )
    data = json.loads(json.dumps(trace.to_dict()))
    again = Trace.from_dict(data)
    assert again == trace
    assert again.spans[0].callsite.stack[0].function == "order_detail"


def test_to_dict_without_profile_and_summary():
    trace = make_trace(profile={"x": 1})
    d = trace.to_dict(include_profile=False)
    assert "profile" not in d and d["has_profile"] is True
    s = trace.summary()
    assert set(s) == {
        "id",
        "method",
        "path",
        "route",
        "view",
        "status",
        "started_at",
        "duration_ms",
        "query_count",
        "query_ms",
        "issue_count",
        "issue_kinds",
        "has_profile",
        "tags",
    }


def test_from_dict_fills_defaults():
    t = Trace.from_dict({"id": "x", "method": "GET", "path": "/"})
    assert t.request["headers"] == {} and t.response["size"] is None
    assert t.framework == "other" and t.spans == [] and t.profile is None


def test_new_id_is_uuid4_hex_unique_and_fork_safe():
    import uuid

    from redline_core.model import new_id

    ids = [new_id() for _ in range(2_000)]  # spans several urandom batches
    assert len(set(ids)) == len(ids)
    for value in ids[:50]:
        parsed = uuid.UUID(value)
        assert parsed.version == 4 and parsed.hex == value
    # a forked child resets the pool (register_at_fork), so it cannot reuse the parent's ids
    from redline_core import model

    new_id()
    pool, pos = model._id_pool, model._id_pos
    left = {pool[i : i + 16].hex() for i in range(pos, len(pool), 16)}
    model._reset_ids()
    assert model._id_pool == b""
    fresh = bytes.fromhex(new_id())
    assert all(bytes.fromhex(h)[9:] != fresh[9:] for h in left)
