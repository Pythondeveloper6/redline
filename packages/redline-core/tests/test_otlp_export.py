from __future__ import annotations

import json

from redline_core import Recorder, Settings
from redline_core.jobs import propagation_headers
from redline_core.model import Issue, Span, Trace
from redline_core.otlp import export, logs_payload, payload
from redline_core.privacy import correlate
from redline_core.tracecontext import parse_traceparent

TRACE_ID = "a" * 32
PARENT_ID = "b" * 16


def _trace():
    span = Span(
        id="sql-1",
        parent_id=None,
        kind="sql",
        name="SELECT order",
        start_ms=1.0,
        duration_ms=12.5,
        data={
            "sql": "SELECT * FROM shop_order WHERE id = 42",
            "fingerprint": "SELECT * FROM shop_order WHERE id = ?",
            "params": {"id": 42},
            "vendor": "mssql",
            "statement_kinds": ["SELECT"],
            "db_alias": "primary",
            "rows": 1,
        },
    )
    issue = Issue(
        id="issue-1",
        kind="sql_error",
        severity="critical",
        title="SQL statement failed",
        detail="driver said SECRET",
        span_ids=["sql-1"],
    )
    return Trace(
        id="recording-1",
        framework="other",
        method="GET",
        path="/orders",
        route="/orders",
        status=500,
        started_at=1_700_000_000.0,
        duration_ms=20.0,
        tags={"otel_trace_id": TRACE_ID, "otel_parent_span_id": PARENT_ID},
        spans=[span],
        issues=[issue],
    )


def test_w3c_traceparent_is_validated():
    parsed = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert parsed == {"trace_id": TRACE_ID, "parent_span_id": PARENT_ID, "sampled": True}
    assert parse_traceparent(f"ff-{TRACE_ID}-{PARENT_ID}-01") is None
    assert parse_traceparent("00-" + "0" * 32 + "-" + PARENT_ID + "-01") is None
    assert parse_traceparent("not-a-traceparent") is None


def test_otlp_exports_sql_spans_and_issue_logs_without_values():
    data = export([_trace()], None)
    spans = data["spans"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 2
    root, sql = spans
    assert root["traceId"] == TRACE_ID and root["parentSpanId"] == PARENT_ID
    assert sql["traceId"] == TRACE_ID and sql["parentSpanId"] == root["spanId"]
    assert sql["kind"] == 3 and sql["name"] == "SELECT"
    attributes = {item["key"]: item["value"] for item in sql["attributes"]}
    assert attributes["db.system"] == {"stringValue": "mssql"}
    assert attributes["db.operation"] == {"stringValue": "SELECT"}
    assert attributes["db.statement"] == {"stringValue": "SELECT * FROM shop_order WHERE id = ?"}
    rendered = json.dumps(data)
    assert "SECRET" not in rendered
    assert "42" not in json.dumps(attributes)

    logs = data["logs"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert len(logs) == 1
    assert logs[0]["severityNumber"] == 17
    assert logs[0]["traceId"] == TRACE_ID
    assert logs[0]["body"] == {"stringValue": "SQL statement failed"}
    assert "SECRET" not in json.dumps(logs)


def test_otlp_can_include_raw_sql_only_on_request():
    assert "42" not in json.dumps(payload([_trace()]))
    assert "42" in json.dumps(payload([_trace()], include_sql=True))
    assert len(logs_payload([_trace()])["resourceLogs"]) == 1


def test_incoming_traceparent_fills_otel_ids_and_outgoing_header():
    trace = Trace("recording-2", "flask", "GET", "/")
    correlate(trace, {"traceparent": f"00-{TRACE_ID}-{PARENT_ID}-01"}, None, Settings())
    assert trace.tags["otel_trace_id"] == TRACE_ID
    assert trace.tags["otel_parent_span_id"] == PARENT_ID

    recorder = Recorder(Settings(sample_rate=0))
    with recorder.recording(method="GET", path="/") as builder:
        headers = propagation_headers()
        assert headers["parent_trace_id"] == builder.id
        parsed = parse_traceparent(headers["traceparent"])
        assert parsed and len(parsed["trace_id"]) == 32
        assert headers["traceparent"] == propagation_headers()["traceparent"]


def test_spans_carry_the_redline_trace_id_attribute():
    spans = export([_trace()], None)["spans"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    for span in spans:
        attrs = {item["key"]: item["value"] for item in span["attributes"]}
        assert attrs["redline.trace_id"] == {"stringValue": "recording-1"}
