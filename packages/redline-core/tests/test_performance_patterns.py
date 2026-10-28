"""New performance findings use deterministic spans and explicit evidence."""

from redline_core.conf import Settings
from redline_core.detectors import patterns
from redline_core.model import Span, new_id
from tests.conftest import make_trace, sql_span


def kinds(trace, **settings):
    return {i.kind for i in patterns.detect(trace, Settings(**settings))}


def test_bulk_writes_ignore_executemany_and_group_by_callsite():
    spans = [sql_span("INSERT INTO items (name) VALUES (%s)", params=[str(i)]) for i in range(6)]
    trace = make_trace(spans=spans)
    assert "repeated_writes" in kinds(trace)
    for s in spans:
        s.data["many"] = True
    assert "repeated_writes" not in kinds(trace)


def test_deep_offset_does_not_confuse_strings_or_unbound_parameters():
    trace = make_trace(spans=[sql_span("SELECT id FROM items ORDER BY id LIMIT 20 OFFSET 10000")])
    assert "deep_offset" in kinds(trace)
    assert not kinds(make_trace(spans=[sql_span("SELECT 'OFFSET 10000' FROM items")]))
    assert not kinds(make_trace(spans=[sql_span("SELECT id FROM items LIMIT 20 OFFSET %s")]))


def test_round_trips_require_count_and_cost_and_avoid_existing_count_budget():
    spans = [sql_span(f"SELECT col_{i} FROM items", duration_ms=10) for i in range(12)]
    trace = make_trace(spans=spans)
    assert "database_round_trips" in kinds(trace)
    assert "database_round_trips" not in kinds(trace, too_many_queries=10)
    assert "database_round_trips" not in kinds(trace, round_trip_ms=200)


def test_pool_wait_and_response_bytes_are_independent():
    span = Span(new_id(), None, "pool_wait", "checkout", 0, 100, data={"pool_size": 5})
    trace = make_trace(spans=[span])
    trace.response["size"] = 2_000_000
    assert {"connection_pool_wait", "large_response"} <= kinds(trace)
    trace.response["size"] = None
    assert "large_response" not in kinds(trace)


def test_repeated_aggregate_scans_require_distinct_projections_and_simple_query():
    trace = make_trace(
        spans=[
            sql_span("SELECT COUNT(*) FROM items WHERE active = %s"),
            sql_span("SELECT SUM(price) FROM items WHERE active = %s"),
        ]
    )
    assert "repeated_scan" in kinds(trace)
    assert "repeated_scan" not in kinds(
        make_trace(spans=[sql_span("SELECT COUNT(*) FROM items")] * 2)
    )


def test_cache_size_requires_explicit_measurement():
    span = Span(
        new_id(), None, "cache", "get", 0, 5, data={"key": "items", "value_bytes": 2_000_000}
    )
    assert "large_cache_entry" in kinds(make_trace(spans=[span]))
    span.data.pop("value_bytes")
    assert "large_cache_entry" not in kinds(make_trace(spans=[span]))


def test_bound_offset_is_observed_without_guessing_redacted_values():
    span = sql_span("SELECT id FROM items LIMIT ? OFFSET ?", params=[20, 10000])
    assert "deep_offset" in kinds(make_trace(spans=[span]))
    span.data["params"] = [20, "[redacted]"]
    assert "deep_offset" not in kinds(make_trace(spans=[span]))


def test_all_synthetic_samples_fire_expected_findings():
    from redline_core.demo import detector_sample

    expected = {
        "writes": "repeated_writes",
        "offset": "deep_offset",
        "round_trips": "database_round_trips",
        "pool": "connection_pool_wait",
        "response": "large_response",
        "aggregates": "repeated_scan",
        "models": "many_orm_instances",
        "cache": "large_cache_entry",
        "json": "json_decode_hotspot",
    }
    for case, kind in expected.items():
        trace = detector_sample(case, Settings())
        assert kind in trace.issue_kinds, (case, trace.issue_kinds)
        assert trace.tags["synthetic_demo"] == "true"


def test_repeated_write_groups_do_not_cross_databases():
    spans = [sql_span("UPDATE items SET price = price") for _ in range(6)]
    for i, span in enumerate(spans):
        span.data["db_alias"] = "primary" if i % 2 else "archive"
    assert "repeated_writes" not in kinds(make_trace(spans=spans))


def test_repeated_offsets_are_one_finding_with_all_evidence():
    spans = [sql_span("SELECT id FROM items LIMIT 5 OFFSET 10000") for _ in range(3)]
    issues = patterns.detect(make_trace(spans=spans), Settings())
    offsets = [issue for issue in issues if issue.kind == "deep_offset"]
    assert len(offsets) == 1 and offsets[0].count == 3
    assert len(offsets[0].span_ids) == 3
