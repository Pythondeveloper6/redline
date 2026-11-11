from __future__ import annotations

import pytest

from redline_core.check.policies import evaluate_budgets, validate_rules


def after(samples):
    return {
        "calls": len(samples),
        "perf": {"samples_ms": samples, "median": 10.0, "p95": 12.0},
        "server": {"queries_max": 3},
    }


def test_p95_budget_needs_min_samples():
    res = evaluate_budgets({"max_p95_ms": 50, "min_samples": 5}, after([10.0, 12.0]))
    assert res == [
        {
            "rule": "max_p95_ms",
            "limit": 50,
            "observed": None,
            "passed": False,
            "reason": "insufficient samples (2 of 5)",
        }
    ]


def test_enough_samples_evaluates_normally():
    res = evaluate_budgets({"max_p95_ms": 50, "min_samples": 2}, after([10.0, 12.0]))
    assert res[0]["passed"] is True and res[0]["observed"] == 12.0


def test_query_budget_ignores_min_samples():
    res = evaluate_budgets({"max_queries": 5, "min_samples": 5}, after([10.0]))
    assert [r["rule"] for r in res] == ["max_queries"] and res[0]["passed"] is True


def test_min_samples_is_not_reported_as_a_rule():
    assert evaluate_budgets({"min_samples": 3}, after([1.0])) == []


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "5"])
def test_min_samples_validated(bad):
    raw = {"budgets": {"max_p95_ms": 10, "min_samples": bad}}
    with pytest.raises(ValueError):
        validate_rules(raw)


def test_min_samples_valid():
    assert validate_rules({"budgets": {"min_samples": 5}})["budgets"] == {"min_samples": 5}
