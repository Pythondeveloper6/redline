import time

from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.detect import Detector
from redline_core.suppressions import matches, validate
from tests.conftest import make_trace, sql_span


def test_source_analysis_debug_gate_and_read_only(store):
    body = {"source": "total = sum(x.duration for x in Event.objects.all())"}
    api = RedlineAPI(store, Settings(), debug=lambda: True)
    code, data = api.dispatch("POST", "api/analysis/source", body=body)
    assert code == 200 and data["findings"][0]["kind"] == "python_aggregation"
    api = RedlineAPI(store, Settings())
    assert api.dispatch("POST", "api/analysis/source", body=body)[0] == 403
    api = RedlineAPI(store, Settings(read_only=True), debug=lambda: True)
    assert api.dispatch("POST", "api/analysis/source", body=body)[0] == 403


def test_arbitrary_project_root_is_rejected(store):
    api = RedlineAPI(store, Settings(), debug=lambda: True)
    assert api.dispatch("POST", "api/analysis/source", body={"root": "/"})[0] == 400


def test_suppression_persists_and_does_not_delete_raw_findings(store):
    trace = make_trace(spans=[sql_span("SELECT id FROM items", [i]) for i in range(6)])
    trace.issues = Detector().detect(trace)
    store.save(trace)
    store.flush()
    api = RedlineAPI(store, Settings())
    body = {
        "kind": "n_plus_one",
        "route": trace.route,
        "reason": "Accepted for a report",
        "expires_at": time.time() + 3600,
    }
    code, rule = api.dispatch("POST", "api/suppressions", body=body)
    assert code == 201, rule
    assert api.dispatch("GET", "api/suppressions")[1]["items"][0]["reason"] == body["reason"]
    code, data = api.dispatch("GET", "api/issues", {"kind": "n_plus_one"})
    assert data["items"][0]["suppressed"]
    assert any(i.kind == "n_plus_one" for i in store.get(trace.id).issues)
    assert api.dispatch("DELETE", f"api/suppressions/{rule['id']}")[0] == 200
    assert not api.dispatch("GET", "api/suppressions")[1]["items"]


def test_expired_and_other_route_suppressions_do_not_match():
    rule = validate(
        {"kind": "slow_query", "route": "/reports", "reason": "accepted", "expires_at": 100}
    )
    assert matches(
        rule, kind="slow_query", route="/reports", fingerprint=None, location=None, now=99
    )
    assert not matches(
        rule, kind="slow_query", route="/reports", fingerprint=None, location=None, now=100
    )
    assert not matches(
        rule, kind="slow_query", route="/other", fingerprint=None, location=None, now=99
    )


def test_coverage_reports_missing_and_single_input_routes(store):
    from redline_core.check.coverage import coverage
    from redline_core.check.suite import Suite

    suite = Suite.from_dict({"requests": [{"path": "/items/1"}, {"path": "/items/2?q=x"}]})
    traces = [
        {"method": "GET", "path": "/items/1", "route": "/items/{id}"},
        {"method": "GET", "path": "/items/2", "route": "/items/{id}"},
        {"method": "GET", "path": "/reports", "route": "/reports"},
    ]
    report = coverage(suite, traces)
    assert [r["status"] for r in report["routes"]] == ["covered", "missing"]


def test_environment_mismatch_is_visible_in_report():
    from redline_core.check.report import build_report
    from redline_core.check.suite import Suite

    suite = Suite.from_dict({"environment": {"dataset": "new"}, "requests": [{"path": "/items"}]})
    report = build_report(
        suite, [], run={}, baseline={"suite": {"environment": {"dataset": "old"}}}
    )
    assert any("dataset differs" in warning for warning in report.summary["warnings"])


def test_health_and_trace_diagnostics_available(store):
    trace = make_trace(spans=[sql_span("SELECT 1")])
    store.save(trace)
    store.flush()
    api = RedlineAPI(store, Settings())
    assert api.dispatch("GET", "api/analysis/health")[1]["observed_traces"] == 1
    assert (
        api.dispatch("GET", f"api/traces/{trace.id}")[1]["diagnostics"]["observed_span_kinds"][
            "sql"
        ]
        == 1
    )


def test_new_issue_kinds_can_be_filtered(store):
    api = RedlineAPI(store, Settings())
    assert api.dispatch("GET", "api/issues", {"kind": "repeated_writes"})[0] == 200


def test_samples_are_explicitly_gated_and_preview_does_not_change_findings(store):
    api = RedlineAPI(store, Settings())
    assert api.dispatch("POST", "api/analysis/demo", body={})[0] == 403
    api = RedlineAPI(store, Settings(analysis_from_ui=True))
    code, result = api.dispatch("POST", "api/analysis/demo", body={})
    assert code == 201 and result["synthetic"]
    trace_id = next(item["id"] for item in result["items"] if item["case"] == "pool")
    before = store.get(trace_id).issues[0].to_dict()
    code, preview = api.dispatch(
        "POST",
        "api/analysis/trace",
        body={"trace_id": trace_id, "settings": {"pool_wait_ms": 1000}},
    )
    assert code == 200 and not preview["issues"]
    assert store.get(trace_id).issues[0].to_dict() == before
    assert (
        api.dispatch(
            "POST",
            "api/analysis/trace",
            body={"trace_id": trace_id, "settings": {"access": "open"}},
        )[0]
        == 400
    )
