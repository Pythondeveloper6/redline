from __future__ import annotations

from redline_core.impact import rank_by_impact

from .test_api import api, db, seeded  # noqa: F401  (fixtures)


def item(id, ms, n, routes=("/a",), suppressed=False):
    return {
        "id": id,
        "observed_total_ms": ms,
        "occurrences": n,
        "routes": list(routes),
        "suppressed": suppressed,
    }


def test_orders_by_observed_ms():
    out = rank_by_impact([item("a", 10, 1), item("b", 50, 1)])
    assert [i["id"] for i in out] == ["b", "a"]


def test_ties_break_on_occurrences():
    out = rank_by_impact([item("a", 10, 1), item("b", 10, 9)])
    assert [i["id"] for i in out] == ["b", "a"]


def test_empty_list():
    assert rank_by_impact([]) == []


def test_suppressed_last_and_flagged():
    out = rank_by_impact([item("s", 999, 5, suppressed=True), item("a", 1, 1)])
    assert [i["id"] for i in out] == ["a", "s"]
    assert out[1]["impact"]["suppressed"] is True


def test_impact_never_marked_additive():
    out = rank_by_impact([item("a", 10, 2, routes=("/a", "/b"))])
    assert out[0]["impact"] == {
        "observed_ms": 10.0,
        "occurrences": 2,
        "routes": 2,
        "basis": "sampled",
        "additive": False,
        "suppressed": False,
    }


def test_api_sort_impact_and_validation(api, seeded):  # noqa: F811
    status, data = api.dispatch("GET", "api/issues", {"sort": "impact"})
    assert status == 200 and all("impact" in i for i in data["items"])
    assert api.dispatch("GET", "api/issues", {"sort": "bogus"})[0] == 400
