"""Final storage capture boundaries, including hostile application metadata."""

import json
from types import SimpleNamespace

import pytest

from redline_core.model import Callsite, Frame, Issue, LogRecord, Span, Trace
from redline_core.trace_safety import enforce_trace_limits, minimal_trace


class Hostile:
    def __str__(self):
        raise AssertionError("str called")

    def __repr__(self):
        raise AssertionError("repr called")


def settings(**changes):
    return SimpleNamespace(
        **(
            {"max_trace_bytes": 1048576, "max_span_data_bytes": 32768, "max_tag_bytes": 2048}
            | changes
        )
    )


def trace():
    return Trace(
        "a" * 32,
        "flask",
        "GET",
        "/normal",
        status=200,
        query_count=12,
        query_ms=8.5,
        spans=[
            Span("b" * 32, None, "sql", "SELECT", 0, 1, data={"sql": "SELECT ?", "params": [1]})
        ],
        tags={"sql_errors": "2", "connection_errors": "1"},
    )


def size(recording):
    return len(json.dumps(recording.to_dict(), ensure_ascii=False, allow_nan=False).encode("utf-8"))


def test_normal_trace_detached_and_unchanged():
    original = trace()
    result = enforce_trace_limits(original, settings())
    assert result is not original
    assert result.to_dict() == original.to_dict()
    result.spans[0].data["params"].append(2)
    assert original.spans[0].data["params"] == [1]


def test_hostile_objects_never_formatted():
    original = trace()
    original.spans[0].data = {"object": Hostile()}
    original.tags["object"] = Hostile()
    original.error = {"message": Hostile(), "type": "ValueError"}
    result = enforce_trace_limits(original, settings())
    assert result.spans[0].data["object"] is None
    assert "object" not in result.tags
    assert result.error["message"] is None
    assert result.truncated["incomplete_evidence"] is True
    size(result)


def test_cyclic_and_deep_data_bounded():
    original = trace()
    cycle = {"self": None}
    cycle["self"] = cycle
    original.spans[0].data = cycle
    result = enforce_trace_limits(original, settings())
    assert result.spans[0].data["self"] is None
    assert result.truncated["capture_limits"] is True


@pytest.mark.parametrize("text", ["x", "🤖", "é", '\\"\n', "\ud800"])
def test_span_data_and_tags_bounded_utf8(text):
    original = trace()
    original.spans[0].data = {"payload": text * 10000}
    original.tags["payload"] = text * 10000
    result = enforce_trace_limits(original, settings(max_span_data_bytes=128, max_tag_bytes=32))
    assert (
        len(json.dumps(result.spans[0].data, separators=(",", ":"), ensure_ascii=True).encode())
        <= 128
    )
    assert len(json.dumps(result.tags["payload"], ensure_ascii=True).encode()) <= 32
    assert result.truncated["capture_limits"]
    size(result)


def test_total_size_matches_storage_encoding_and_preserves_counters():
    original = trace()
    original.spans *= 100
    original.logs = [LogRecord(0, "INFO", "app", "x" * 10000)] * 50
    original.profile = {"big": "x" * 100000}
    result = enforce_trace_limits(original, settings(max_trace_bytes=4096))
    assert size(result) <= 4096
    assert result.truncated["incomplete_evidence"] is True
    assert result.query_count == 12
    assert result.query_ms == 8.5
    assert result.tags.get("sql_errors", str(result.truncated.get("sql_errors"))) == "2"
    assert result.logs_dropped == 50


def test_minimal_fallback_omits_private_fields():
    original = trace()
    original.id = "private-id"
    original.path = "/private-user"
    original.request = {"body": "secret"}
    original.tags["user_id"] = "private"
    original.error = {"type": "ValueError", "message": "secret", "frames": ["private"]}
    result = minimal_trace(original, "privacy_failure")
    encoded = json.dumps(result.to_dict())
    assert "private" not in encoded
    assert "secret" not in encoded
    assert result.tags == {}
    assert result.path == "/[capture-unavailable]"
    assert result.error == {"type": "ValueError"}
    assert result.truncated["sql_errors"] == 2
    assert result.truncated["reason"] == "privacy_failure"


def test_giant_path_falls_back_to_safe_minimal():
    original = trace()
    original.path = "sensitive" * 10000
    result = enforce_trace_limits(original, settings(max_trace_bytes=4096))
    assert result.path == "/[capture-unavailable]"
    assert result.truncated["reason"] == "trace_budget"
    assert size(result) <= 4096


def test_invalid_capture_never_returns_original():
    original = trace()
    original.query_count = Hostile()
    result = enforce_trace_limits(original, settings())
    assert result is not original
    assert result.path == "/[capture-unavailable]"
    assert result.query_count == 0
    assert result.truncated["reason"] == "invalid_capture"


def test_no_minimal_trace_fits_returns_none():
    assert enforce_trace_limits(trace(), settings(max_trace_bytes=4)) is None


def test_invalid_reason_is_not_captured():
    assert minimal_trace(trace(), "secret-canary").truncated["reason"] == "capture_failure"


def test_source_errors_and_issue_values_are_bounded():
    original = trace()
    original.spans[0].callsite = Callsite("a.py", 1, "fn", "x" * 100000, (Frame("a.py", 1, "fn"),))
    original.issues = [
        Issue("issue", "sql_error", "warning", "title", "x" * 100000, extra={"bad": Hostile()})
    ]
    result = enforce_trace_limits(original, settings(max_trace_bytes=4096))
    assert size(result) <= 4096
    assert result.truncated["incomplete_evidence"]


def test_model_cycles_fail_closed():
    original = trace()
    original.spans[0].callsite = original.spans[0]
    result = enforce_trace_limits(original, settings())
    assert result is not original
    assert result.truncated["incomplete_evidence"]


def test_truncated_sql_evidence_cannot_be_explained():
    original = trace()
    original.spans[0].data.update(explainable=True, params_captured=True, params=["large" * 1000])
    result = enforce_trace_limits(original, settings(max_span_data_bytes=128))
    data = result.spans[0].data
    assert data.get("explainable") is False
    assert data.get("params_captured") is False
    assert data.get("capture_incomplete") is True
    assert len(json.dumps(data, ensure_ascii=True, separators=(",", ":"))) <= 128


def test_reserved_failure_counts_survive_tag_item_cap():
    original = trace()
    original.tags = {f"tag{i}": "value" for i in range(1000)} | {"sql_errors": "27"}
    result = enforce_trace_limits(original, settings())
    assert result.tags["sql_errors"] == "27"
    assert len(result.tags) <= 102
    assert result.truncated["incomplete_evidence"]


def test_minimal_rejects_huge_numeric_values_without_conversion():
    original = trace()
    original.status = 10**10000
    original.duration_ms = float("inf")
    result = minimal_trace(original, "privacy_failure")
    assert result.status == 0
    assert result.duration_ms == 0
    size(result)


def test_changed_sql_name_disables_explain_and_clears_params():
    original = trace()
    original.spans[0].name = "x" * 100000
    original.spans[0].data.update(explainable=True, params_captured=True)
    result = enforce_trace_limits(original, settings())
    data = result.spans[0].data
    assert data["capture_incomplete"] is True
    assert data["explainable"] is False
    assert data["params"] is None


def test_body_and_profile_representations_both_obey_budget():
    original = trace()
    original.path = "x" * 4000
    result = enforce_trace_limits(original, settings(max_trace_bytes=4096))
    for include_profile in (True, False):
        encoded = json.dumps(
            result.to_dict(include_profile=include_profile), ensure_ascii=False, allow_nan=False
        ).encode()
        assert len(encoded) <= 4096
