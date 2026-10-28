"""Response rules and budgets must agree across saved suites and check reports."""

import math

import pytest

from redline_core.check.diff import compare_captures, json_diff
from redline_core.check.policies import evaluate_budgets
from redline_core.check.report import compare_request
from redline_core.check.suite import Suite, SuiteError


def suite(**rules):
    return Suite.from_dict({"requests": [{"path": "/items", **rules}]})


def test_keyed_array_reordering_preserves_element_changes():
    a = {"items": [{"id": 1, "price": 10}, {"id": 2, "price": 20}]}
    b = {"items": [{"id": 2, "price": 21}, {"id": 1, "price": 10}]}
    diffs = list(json_diff(a, b, array_keys={"items": "id"}))
    assert len(diffs) == 1
    assert diffs[0]["path"] == "items[1].price"
    assert diffs[0]["before"] == 20
    assert diffs[0]["after"] == 21


def test_keyed_array_add_remove_and_wildcard_ignore():
    a = {"items": [{"id": 1, "ts": "a"}, {"id": 2}]}
    b = {"items": [{"id": 3}, {"id": 1, "ts": "b"}]}
    req = suite(array_keys={"items": "id"}, ignore=["items[*].ts"]).requests[0]
    diffs = list(json_diff(a, b, array_keys=req.array_keys, ignore=req.ignore_patterns()))
    assert [d["kind"] for d in diffs] == ["removed", "added"]


@pytest.mark.parametrize(
    "items", [[{"id": 1}, {"id": 1}], [{}], [1], [{"id": True}], [{"id": None}]]
)
def test_bad_identity_is_a_difference(items):
    diffs = list(json_diff(items, items, array_keys={"$": "id"}))
    assert diffs[0]["kind"] == "invalid_identity"


def test_numeric_rules_do_not_hide_types_or_missing_fields():
    rules = {"price": {"relative": 0.01}, "duration": {"absolute": 0.1}}
    assert not list(json_diff({"price": 100}, {"price": 101}, tolerances=rules))
    assert list(json_diff({"price": 0}, {"price": 0.01}, tolerances=rules))
    assert list(json_diff({"price": 1}, {"price": True}, tolerances=rules))
    assert list(json_diff({"price": 100}, {}, tolerances=rules))
    assert not list(json_diff({"duration": 1.0}, {"duration": 1.05}, tolerances=rules))


@pytest.mark.parametrize(
    "rules",
    [
        {"budgets": {"max_queries": -1}},
        {"budgets": {"unknown": 1}},
        {"budgets": {"max_queries": True}},
        {"budgets": {"max_queries": math.inf}},
        {"tolerances": {"price": {"relative": math.nan}}},
        {"tolerances": {"price": {"typo": 1}}},
        {"array_keys": {"items[": "id"}},
        {"array_keys": {"items": ""}},
        {"budgets": {"forbidden_issues": "n_plus_one"}},
    ],
)
def test_invalid_policy_rejected(rules):
    with pytest.raises(SuiteError):
        suite(**rules)


def test_rules_round_trip_json_and_toml(tmp_path):
    original = suite(
        array_keys={"items": "id"},
        tolerances={"total": {"absolute": 0.01}},
        budgets={"max_queries": 5, "forbidden_issues": ["n_plus_one"]},
    )
    for ext, content in (("json", original.to_json()), ("toml", original.to_toml())):
        path = tmp_path / f"suite.{ext}"
        path.write_text(content)
        assert Suite.from_file(path).to_dict() == original.to_dict()


def test_budget_metrics_missing_and_broken_never_pass():
    budgets = {"max_queries": 5, "max_p95_ms": 100, "forbidden_issues": ["n_plus_one"]}
    assert all(not r["passed"] for r in evaluate_budgets(budgets, {}))
    after = {
        "broken": True,
        "perf": {"samples_ms": [1], "p95": 1},
        "server": {"queries_max": 0, "issues": []},
    }
    assert all(not r["passed"] for r in evaluate_budgets(budgets, after))


def test_budget_uses_maximum_queries_and_works_without_baseline():
    req = suite(budgets={"max_queries": 5}).requests[0]
    after = {"server": {"queries_max": 6, "queries_median": 1}}
    report = compare_request(req, None, after, suite().thresholds)
    assert report["budgets"][0]["passed"] is False


def test_capture_rules_are_returned_for_inspection():
    a = {"body_kind": "json", "body": {"items": [{"id": 1}, {"id": 2}]}}
    b = {"body_kind": "json", "body": {"items": [{"id": 2}, {"id": 1}]}}
    compared = compare_captures(a, b, array_keys={"items": "id"})
    assert compared["diff_total"] == 0


def test_report_budget_fails_json_markdown_and_junit():
    from redline_core.check.report import build_report

    s = suite(budgets={"max_queries": 5})
    after = {"server": {"queries_max": 6, "queries_median": 1}, "calls": 1}
    e = compare_request(s.requests[0], None, after, s.thresholds)
    report = build_report(s, [e], run={"id": "run"}, baseline={})
    assert report.exit_code() == 1
    assert "budget" in report.to_json()
    assert "budget max_queries:" in report.to_markdown()
    assert "budget max_queries:" in report.to_terminal()
    assert "endpoint budget failed" in report.to_junit()
    assert report.with_fail_on(()).exit_code() == 0


def test_api_rules_saved_reloaded_and_exported(store):
    import json

    from redline_core.api import RedlineAPI
    from redline_core.conf import Settings

    api = RedlineAPI(store, Settings())
    s = suite(
        array_keys={"items": "id"},
        tolerances={"total": {"absolute": 0.01}},
        budgets={"max_queries": 5},
    ).to_dict()
    code, saved = api.dispatch(
        "POST", "api/checks/suites", body=json.dumps({"name": "rules", "suite": s}).encode()
    )
    assert code == 201, saved
    code, loaded = api.dispatch("GET", f"api/checks/suites/{saved['id']}")
    assert loaded["suite"]["requests"][0]["array_keys"] == {"items": "id"}
    code, exported = api.dispatch("GET", f"api/checks/suites/{saved['id']}/export")
    assert code == 200
    assert b"array_keys" in exported.body


def test_runner_applies_response_rules_using_in_process_http():
    import httpx

    from redline_core.check.runner import run_suite

    s = suite(array_keys={"items": "id"}, tolerances={"total": {"absolute": 0.1}}).replace(
        repeat=3, warmup=0
    )
    before = {"items": [{"id": 1}, {"id": 2}], "total": 1.0}
    after = {"items": [{"id": 2}, {"id": 1}], "total": 1.05}
    old = httpx.MockTransport(lambda r: httpx.Response(200, json=before))
    new = httpx.MockTransport(lambda r: httpx.Response(200, json=after))
    baseline = run_suite(s, base_url="http://app.test", kind="baseline", transport=old)
    report = run_suite(
        s, base_url="http://app.test", kind="compare", transport=new, baseline=baseline
    )
    assert report.requests[0]["response"]["verdict"] == "same"
    assert report.requests[0]["response"]["rules"]["array_keys"] == {"items": "id"}


def test_current_volatility_tracks_identity_when_array_order_changes():
    req = suite(array_keys={"items": "id"}).requests[0]
    before = {
        "response": {
            "body_kind": "json",
            "body": {"items": [{"id": 1, "value": 10}, {"id": 2, "value": 20}]},
        }
    }
    after = {
        "response": {
            "body_kind": "json",
            "body": {"items": [{"id": 2, "value": 21}, {"id": 1, "value": 11}]},
        },
        "volatile": ["items[0].value"],
    }
    compared = compare_request(req, before, after, suite().thresholds)
    assert compared["response"]["verdict"] == "changed"
    differences = compared["response"]["diff"]
    assert len(differences) == 1
    assert differences[0]["before"] == 10 and differences[0]["after"] == 11
