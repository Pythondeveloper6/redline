from __future__ import annotations

import json
from xml.etree import ElementTree as ET

import pytest

from redline_core.check import Baseline, Report, Suite, build_report, compare_request, parse_fail_on
from redline_core.check.diff import capture
from redline_core.check.stats import summarize
from redline_core.check.suite import Thresholds

TH = Thresholds()


def req(**kw):
    data = {"name": "r", "path": "/r", **kw}
    return Suite.from_dict({"name": "s", "requests": [data]}).requests[0]


def resp(body=None, status=200, ctype="application/json", **headers):
    raw = json.dumps({"ok": True} if body is None else body).encode()
    return capture(status, {"Content-Type": ctype, **headers}, raw)


def entry(samples, *, failed=0, server=None, response="default", volatile=(), idx=0):
    calls = len(samples) + failed
    return {
        "idx": idx,
        "name": "r",
        "calls": calls,
        "failed": failed,
        "broken": calls > 0 and failed * 2 >= calls,
        "perf": {"samples_ms": list(samples), **summarize(samples), "failed": failed},
        "server": server,
        "response": resp() if response == "default" else response,
        "volatile": list(volatile),
        "volatile_headers": [],
    }


STEADY = [10.0, 10.2, 9.9, 10.1, 10.0, 9.8, 10.3, 10.1, 10.0, 9.9] * 2


def scaled(factor):
    return [v * factor for v in STEADY]


# ----------------------------------------------------------------------- perf verdicts
def test_perf_slower_faster_same():
    slower = compare_request(req(), entry(STEADY), entry(scaled(1.5)), TH)["perf"]
    assert slower["verdict"] == "slower" and slower["reason"] == "time"
    assert slower["p_value"] < 0.01 and slower["change_pct"] == pytest.approx(50, abs=0.5)
    assert slower["change_ms"] == pytest.approx(5.0, abs=0.1)
    faster = compare_request(req(), entry(STEADY), entry(scaled(0.5)), TH)["perf"]
    assert faster["verdict"] == "faster" and faster["reason"] == "time"
    same = compare_request(req(), entry(STEADY), entry(list(STEADY)), TH)["perf"]
    assert same["verdict"] == "same" and same["reason"] is None and same["p_value"] > 0.5
    assert same["before"]["samples_ms"] == STEADY and same["after"]["n"] == len(STEADY)


def test_perf_needs_both_thresholds():
    # +15 % but only +1.5 ms: below slower_ms = 2
    assert (
        compare_request(req(), entry(STEADY), entry(scaled(1.15)), TH)["perf"]["verdict"] == "same"
    )
    # +3 ms but only +3 % of 100 ms: below slower_pct = 10
    big = [v * 10 for v in STEADY]
    assert (
        compare_request(req(), entry(big), entry([v + 3 for v in big]), TH)["perf"]["verdict"]
        == "same"
    )
    loose = Thresholds(slower_pct=1, slower_ms=1)
    assert (
        compare_request(req(), entry(big), entry([v + 3 for v in big]), loose)["perf"]["verdict"]
        == "slower"
    )


def test_perf_needs_significance():
    # two samples each: never significant at p < 0.01, however large the change
    out = compare_request(req(), entry([10.0, 10.1]), entry([50.0, 50.2]), TH)["perf"]
    assert out["verdict"] == "same" and out["p_value"] > 0.01


def test_perf_noisy():
    noisy = [1.0, 30.0, 2.0, 25.0, 1.5, 40.0, 1.0, 35.0]
    out = compare_request(req(), entry(STEADY), entry(noisy), TH)["perf"]
    assert out["verdict"] == "noisy" and out["reason"] == "cv" and "repeat" in out["note"]
    assert compare_request(req(), entry(noisy), entry(STEADY), TH)["perf"]["verdict"] == "noisy"


def test_perf_queries_reason():
    before = entry(STEADY, server={"queries_median": 3})
    after = entry(list(STEADY), server={"queries_median": 4})
    out = compare_request(req(), before, after, TH)["perf"]
    assert out["verdict"] == "slower" and out["reason"] == "queries"
    allowed = Thresholds(max_query_increase=1)
    assert compare_request(req(), before, after, allowed)["perf"]["verdict"] == "same"
    both = compare_request(req(), before, entry(scaled(2), server={"queries_median": 9}), TH)[
        "perf"
    ]
    assert both["verdict"] == "slower" and both["reason"] == "queries" and both["note"]
    # fewer queries alone is not "faster"
    fewer = compare_request(req(), before, entry(list(STEADY), server={"queries_median": 1}), TH)
    assert fewer["perf"]["verdict"] == "same"
    # one side without server metrics: no query verdict
    half = compare_request(req(), before, entry(list(STEADY)), TH)
    assert half["perf"]["verdict"] == "same" and half["server"]["note"]


def test_perf_na():
    assert (
        compare_request(req(perf=False), entry(STEADY), entry(STEADY), TH)["perf"]["verdict"]
        == "n/a"
    )
    na = compare_request(req(), None, entry(STEADY), TH)["perf"]
    assert na["verdict"] == "n/a" and na["reason"] is None and na["note"] == "not in the baseline"
    failing = compare_request(req(), entry(STEADY), entry([1.0], failed=3), TH)["perf"]
    assert failing["verdict"] == "n/a" and "failed" in failing["note"]


# ----------------------------------------------------------------------- response verdicts
def rv(before, after, **kw):
    return compare_request(req(**kw), before, after, TH)["response"]


def test_response_same_and_changed():
    assert rv(entry(STEADY), entry(STEADY))["verdict"] == "same"
    changed = rv(entry(STEADY, response=resp({"a": 1})), entry(STEADY, response=resp({"a": 2})))
    assert changed["verdict"] == "changed" and changed["diff"][0]["path"] == "a"
    assert changed["diff_total"] == 1
    status = rv(entry(STEADY, response=resp(status=200)), entry(STEADY, response=resp(status=201)))
    assert status["verdict"] == "changed" and status["status"] == [200, 201]
    ctype = rv(entry(STEADY), entry(STEADY, response=resp(ctype="text/json")))
    assert ctype["verdict"] == "changed" and ctype["diff"][0]["path"] == "header:content-type"
    redirect = rv(
        entry(STEADY, response=resp(status=200)), entry(STEADY, response=resp(status=302))
    )
    assert redirect["verdict"] == "changed"


def test_response_ignore_and_volatile():
    before = entry(STEADY, response=resp({"a": 1, "now": 1, "rid": "x"}), volatile=["now"])
    after = entry(STEADY, response=resp({"a": 1, "now": 2, "rid": "y"}))
    out = rv(before, after, ignore=["rid"])
    assert out["verdict"] == "same"
    assert {"path": "now", "reason": "volatile"} in out["ignored"]
    assert {"path": "rid", "reason": "ignore"} in out["ignored"]
    # volatility seen during the compare run itself also counts
    after2 = entry(STEADY, response=resp({"a": 1, "now": 2, "rid": "x", "t": 5}), volatile=["t"])
    before2 = entry(STEADY, response=resp({"a": 1, "now": 1, "rid": "x", "t": 4}), volatile=["now"])
    assert rv(before2, after2)["verdict"] == "same"


def test_response_broken():
    to_500 = rv(entry(STEADY, response=resp(status=200)), entry(STEADY, response=resp(status=500)))
    assert to_500["verdict"] == "broken" and "200 → 500" in to_500["reason"]
    to_404 = rv(entry(STEADY, response=resp(status=302)), entry(STEADY, response=resp(status=404)))
    assert to_404["verdict"] == "broken"
    was_bad = rv(entry(STEADY, response=resp(status=404)), entry(STEADY, response=resp(status=500)))
    assert was_bad["verdict"] == "changed"  # 4xx → 5xx is a change, not a new breakage
    expect = rv(entry(STEADY), entry(STEADY, response=resp(status=201)), expect_status=200)
    assert expect["verdict"] == "broken" and "expected status 200" in expect["reason"]
    assert rv(entry(STEADY), entry(STEADY), expect_status=[200, 201])["verdict"] == "same"
    failing = rv(entry(STEADY), entry([1.0, 1.0], failed=2, response=None))
    assert failing["verdict"] == "broken" and "failed calls" in failing["reason"]
    # failures make a request broken even with response = false
    off = rv(entry(STEADY), entry([1.0], failed=3, response=None), response=False)
    assert off["verdict"] == "broken"
    assert rv(entry(STEADY), entry(STEADY), response=False)["verdict"] == "n/a"
    assert rv(None, entry(STEADY))["verdict"] == "n/a"
    healed = rv(entry([], failed=4, response=None), entry(STEADY))
    assert healed["verdict"] == "changed"


# ----------------------------------------------------------------------- summary
def make_report(pairs, fail_on=("slower", "changed", "broken"), suite_requests=None):
    names = [f"r{i}" for i in range(len(pairs))]
    suite = Suite.from_dict(
        {"name": "s", "requests": suite_requests or [{"name": n, "path": f"/{n}"} for n in names]}
    )
    entries = []
    for i, (r, (b, a)) in enumerate(zip(suite.requests, pairs, strict=True)):
        b = {**b, "name": r.name, "idx": i} if b else None
        entries.append(compare_request(r, b, {**a, "name": r.name, "idx": i}, TH))
    baseline = Baseline(
        id="b1",
        suite={},
        requests=[],
        created_at=1767000000.0,
        label="before",
        git={"sha": "abcdef1234"},
    )
    run = {"id": "c1", "git": None, "created_at": 1767000100.0, "base_url": "http://x"}
    return build_report(suite, entries, run=run, baseline=baseline, fail_on=fail_on)


def test_summary_geometric_mean_and_counts():
    rep = make_report(
        [
            (entry(STEADY), entry(scaled(0.5))),  # 2× faster
            (entry(STEADY), entry(scaled(2.0))),  # 2× slower
            (entry(STEADY), entry(scaled(0.5))),  # 2× faster
            (entry(STEADY), entry([1.0], failed=1)),  # failures: excluded from the ratio
        ]
    )
    s = rep.summary
    assert s["speed_requests"] == 3
    assert s["speed_ratio"] == pytest.approx(0.5 ** (1 / 3), abs=1e-3)
    assert s["speed_change_pct"] == pytest.approx((0.5 ** (1 / 3) - 1) * 100, abs=0.1)
    assert s["perf"] == {"faster": 2, "slower": 1, "same": 0, "noisy": 0, "n/a": 1}
    assert s["response"]["broken"] == 1 and s["response"]["same"] == 3
    assert [w["name"] for w in s["wins"]] == ["r0", "r2"]
    assert [w["name"] for w in s["regressions"]] == ["r1"]
    assert s["failing"] == ["r1", "r3"] and s["failed"] == 2 and s["passed"] == 2
    assert not s["server_side"] and s["queries_change"] is None


def test_summary_queries_total():
    rep = make_report(
        [
            (
                entry(STEADY, server={"queries_median": 10}),
                entry(STEADY, server={"queries_median": 2}),
            ),
            (
                entry(STEADY, server={"queries_median": 5}),
                entry(STEADY, server={"queries_median": 5}),
            ),
        ]
    )
    s = rep.summary
    assert (s["queries_before"], s["queries_after"], s["queries_change"]) == (15, 7, -8)
    assert s["server_side"]


def test_server_section_items():
    b = entry(STEADY, server={
        "queries_median": 2, "sample_trace_id": "tb",
        "sql": {"f1": {"count": 1, "sql": "SELECT a"}, "gone": {"count": 1, "sql": "SELECT g"}},
        "issues": [{"kind": "duplicate", "fingerprint": "f1", "title": "dup"}],
    })  # fmt: skip
    a = entry(STEADY, server={
        "queries_median": 2, "sample_trace_id": "ta",
        "sql": {"f1": {"count": 1, "sql": "SELECT a"}, "new": {"count": 5, "sql": "SELECT n"}},
        "issues": [{"kind": "n_plus_one", "fingerprint": "new", "title": "N+1 on items"}],
    })  # fmt: skip
    srv = compare_request(req(), b, a, TH)["server"]
    assert srv["new_issues"] == [
        {
            "kind": "n_plus_one",
            "title": "N+1 on items",
            "fingerprint": "new",
            "sample_trace_id": "ta",
        }
    ]
    assert srv["fixed_issues"] == [
        {"kind": "duplicate", "title": "dup", "fingerprint": "f1", "sample_trace_id": "tb"}
    ]
    assert srv["new_sql"] == [
        {"fingerprint": "new", "sql": "SELECT n", "count": 5, "sample_trace_id": "ta"}
    ]
    assert srv["gone_sql"] == [
        {"fingerprint": "gone", "sql": "SELECT g", "count": 1, "sample_trace_id": "tb"}
    ]
    assert srv["before"] is b["server"] and srv["queries_change"] == 0


# ----------------------------------------------------------------------- fail-on, exit
def test_parse_fail_on():
    assert parse_fail_on(None) == ("slower", "changed", "broken")
    assert parse_fail_on("slower, broken,slower") == ("slower", "broken")
    assert parse_fail_on("") == () and parse_fail_on("none") == ()
    assert parse_fail_on(["noisy"]) == ("noisy",)
    with pytest.raises(ValueError, match="unknown verdict 'slow'"):
        parse_fail_on("slow")


def test_exit_code_and_with_fail_on():
    rep = make_report([(entry(STEADY), entry(scaled(2.0))), (entry(STEADY), entry(STEADY))])
    assert rep.exit_code() == 1
    assert rep.with_fail_on(("changed", "broken")).exit_code() == 0
    assert rep.with_fail_on(()).summary["passed"] == 2
    noisy = make_report([(entry(STEADY), entry([1.0, 30.0, 2.0, 25.0, 1.5, 40.0]))])
    assert noisy.exit_code() == 0 and noisy.with_fail_on(("noisy",)).exit_code() == 1


# ----------------------------------------------------------------------- rendering
def sample_report():
    return make_report(
        [
            (entry(STEADY), entry(scaled(0.5))),
            (
                entry(STEADY, response=resp({"a": 1, "b": "<x>"})),
                entry(STEADY, response=resp({"a": 2})),
            ),
            (entry(STEADY), entry([1.0], failed=3, response=None)),
        ],
        suite_requests=[
            {"name": "fast | one", "path": "/a"},
            {"name": "changed", "path": "/b"},
            {"name": "broken", "path": "/c"},
        ],
    )


def test_json_round_trip():
    rep = sample_report()
    data = json.loads(rep.to_json())
    assert data["format"] == "redline-check-report" and data["version"] == 1
    assert set(data) >= {"suite", "baseline", "run", "summary", "requests"}
    assert set(data["requests"][0]) >= {"name", "method", "path", "perf", "server", "response"}
    assert set(data["requests"][0]["perf"]) >= {
        "verdict",
        "before",
        "after",
        "change_ms",
        "change_pct",
        "p_value",
        "reason",
    }
    assert set(data["requests"][0]["server"]) >= {
        "before",
        "after",
        "queries_change",
        "new_issues",
        "fixed_issues",
        "new_sql",
        "gone_sql",
    }
    assert set(data["requests"][0]["response"]) >= {
        "verdict",
        "status",
        "diff",
        "diff_total",
        "text_diff",
        "ignored",
    }
    again = Report.from_dict(data)
    assert again.to_dict() == rep.to_dict()
    from redline_core.check import ReportError

    with pytest.raises(ReportError):
        Report.from_dict({"format": "x"})
    with pytest.raises(ReportError, match="version"):
        Report.from_dict({**data, "version": 9})


def test_terminal():
    rep = sample_report()
    plain = rep.to_terminal()
    assert "\x1b[" not in plain
    assert "faster" in plain and "changed" in plain and "broken" in plain
    assert "~ a: 1 → 2" in plain and "- b:" in plain
    assert "FAIL: 2 of 3 requests" in plain and "'before' abcdef12" in plain
    colored = rep.to_terminal(color=True)
    assert "\x1b[32m" in colored and "\x1b[1;31m" in colored
    assert "PASS" in rep.with_fail_on(()).to_terminal()


def test_markdown_shape():
    md = sample_report().to_markdown()
    lines = md.splitlines()
    assert lines[0] == "### Redline refactor check: s"
    assert lines[2].startswith("**FAIL** – 2 of 3 requests")
    assert "| Request | Perf | Median before → after | Δ | Queries | Response |" in lines
    assert "| fast \\| one | faster |" in md  # pipes escaped
    assert md.count("<details>") == md.count("</details>") == 2
    assert "broken: ≥ 50% failed calls" in md
    assert "<summary>changed – changed</summary>" in md
    assert "```diff" in md and "~ a: 1 → 2" in md
    assert "Server-side metrics unavailable" in md


def test_junit_parses():
    xml = sample_report().to_junit()
    root = ET.fromstring(xml)
    assert root.tag == "testsuites"
    ts = root.find("testsuite")
    assert ts.get("name") == "s" and ts.get("tests") == "3" and ts.get("failures") == "2"
    cases = ts.findall("testcase")
    assert [c.get("name") for c in cases] == ["fast | one", "changed", "broken"]
    assert cases[0].find("failure") is None
    f = cases[1].find("failure")
    assert f is not None and f.get("type") == "changed" and "response changed" in f.get("message")
    assert "~ a: 1 → 2" in f.text
    assert cases[2].find("failure").get("type") == "broken"
    assert float(cases[0].get("time")) > 0
    passing = ET.fromstring(sample_report().with_fail_on(()).to_junit())
    assert passing.find("testsuite").get("failures") == "0"


def test_type_change_diff_quotes_strings():
    from redline_core.check.report import _diff_lines

    e = {
        "response": {
            "diff": [{"path": "n", "kind": "type", "before": 200, "after": "200"}],
            "diff_total": 1,
            "text_diff": None,
        }
    }
    assert _diff_lines(e, 10) == ['~ n: 200 → "200" (type)']
