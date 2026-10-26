from __future__ import annotations

from redline_core.conf import Settings
from redline_core.detect import Detector, FixSuggester, GenericSuggester, detect, issue_id
from redline_core.model import Suggestion

from .conftest import make_callsite, make_trace, sql_span

CHILD = 'SELECT * FROM "shop_orderitem" WHERE "order_id" = %s'


def n_plus_one_spans(n, callsite=None, duration=1.0):
    cs = callsite or make_callsite()
    return [sql_span(CHILD, [i], callsite=cs, duration_ms=duration) for i in range(n)]


def kinds(issues):
    return [i.kind for i in issues]


def test_n_plus_one_threshold():
    assert detect(make_trace(spans=n_plus_one_spans(4))) == []
    issues = detect(make_trace(spans=n_plus_one_spans(5)))
    assert kinds(issues) == ["n_plus_one"]
    issue = issues[0]
    assert issue.count == 5 and issue.severity == "warning"
    assert issue.title == "N+1: 5 queries on shop_orderitem from shop/views.py:41"
    assert issue.callsite.location == "shop/views.py:41"
    assert issue.suggestion is not None and issue.suggestion.confidence == "low"


def test_n_plus_one_needs_same_callsite():
    spans = n_plus_one_spans(3, make_callsite(line=1)) + n_plus_one_spans(3, make_callsite(line=2))
    assert "n_plus_one" not in kinds(detect(make_trace(spans=spans)))


def test_n_plus_one_severity_critical_by_count_or_time():
    assert detect(make_trace(spans=n_plus_one_spans(20)))[0].severity == "critical"
    assert detect(make_trace(spans=n_plus_one_spans(5, duration=30)))[0].severity == "critical"


def test_writes_are_not_n_plus_one():
    spans = [sql_span("UPDATE t SET a = %s", [i], callsite=make_callsite()) for i in range(10)]
    assert detect(make_trace(spans=spans)) == []


def test_issue_ids_are_stable_across_traces():
    a = detect(make_trace(spans=n_plus_one_spans(6)))[0]
    b = detect(make_trace(spans=n_plus_one_spans(9)))[0]
    assert a.id == b.id == issue_id("n_plus_one", a.fingerprint, "shop/views.py:41")
    assert len(a.id) == 16
    c = detect(make_trace(spans=n_plus_one_spans(6, make_callsite(line=99))))[0]
    assert c.id != a.id


def test_duplicates():
    cs = make_callsite()
    spans = [sql_span("SELECT * FROM cfg WHERE k = %s", ["a"], callsite=cs) for _ in range(2)]
    spans.append(sql_span("SELECT * FROM cfg WHERE k = %s", ["b"], callsite=cs))
    issues = detect(make_trace(spans=spans))
    assert kinds(issues) == ["duplicate_query"]
    assert issues[0].count == 2 and issues[0].severity == "info"
    assert issues[0].title.startswith("Duplicate query ×2 on cfg")


def test_duplicates_inside_an_n_plus_one_are_not_reported_twice():
    cs = make_callsite()
    spans = [sql_span(CHILD, [1], callsite=cs) for _ in range(6)]
    assert kinds(detect(make_trace(spans=spans))) == ["n_plus_one"]


def test_duplicate_severity_warning_at_five():
    spans = [sql_span("SELECT 1 FROM x", []) for _ in range(5)]
    issues = detect(make_trace(spans=spans), Settings(n_plus_one_threshold=50))
    assert issues[0].severity == "warning"


def test_slow_query_grouping_and_severity():
    cs = make_callsite()
    spans = [
        sql_span("SELECT * FROM big WHERE a = %s", [1], duration_ms=150, callsite=cs),
        sql_span("SELECT * FROM big WHERE a = %s", [2], duration_ms=120, callsite=cs),
        sql_span("SELECT * FROM fast", [], duration_ms=99.9),
    ]
    issues = detect(make_trace(spans=spans))
    assert kinds(issues) == ["slow_query"]
    assert issues[0].count == 2 and issues[0].severity == "warning"
    assert issues[0].title == "Slow query: 150 ms on big from shop/views.py:41"
    big = detect(make_trace(spans=[sql_span("SELECT 1", [], duration_ms=600)]))
    assert big[0].severity == "critical"


def test_too_many_queries_per_route():
    spans = [sql_span(f"SELECT * FROM t{i}", []) for i in range(50)]
    issues = detect(make_trace(spans=spans))
    assert kinds(issues) == ["too_many_queries"]
    assert issues[0].severity == "warning" and issues[0].count == 50
    other = detect(make_trace(spans=spans, route="/other/"))[0]
    assert other.id != issues[0].id
    assert detect(make_trace(spans=spans * 2))[0].severity == "critical"


def test_suggesters_run_in_order_and_failures_are_swallowed():
    calls = []

    class Broken:
        def suggest(self, issue, spans):
            calls.append("broken")
            raise RuntimeError("boom")

    class Django:
        def suggest(self, issue, spans):
            calls.append(("django", len(spans)))
            if issue.kind == "n_plus_one":
                return Suggestion(
                    summary="prefetch", code=".prefetch_related('items')", confidence="high"
                )
            return None

    assert isinstance(Django(), FixSuggester)
    det = Detector(Settings(), [Broken(), Django()])
    issue = det.detect(make_trace(spans=n_plus_one_spans(5)))[0]
    assert issue.suggestion.summary == "prefetch"
    assert calls == ["broken", ("django", 5)]

    det = Detector(Settings(), generic_fallback=False)
    assert det.detect(make_trace(spans=n_plus_one_spans(5)))[0].suggestion is None
    det.add_suggester(Django())
    assert det.detect(make_trace(spans=n_plus_one_spans(5)))[0].suggestion.confidence == "high"


def test_issue_ordering_by_severity():
    spans = n_plus_one_spans(20) + [sql_span("SELECT 1 FROM x", []) for _ in range(2)]
    assert kinds(detect(make_trace(spans=spans))) == ["n_plus_one", "duplicate_query"]


def test_generic_suggester_covers_all_kinds():
    g = GenericSuggester()
    from redline_core.model import Issue

    for kind in ("n_plus_one", "duplicate_query", "slow_query", "too_many_queries"):
        assert (
            g.suggest(Issue(id="x", kind=kind, severity="info", title="", detail=""), [])
            is not None
        )
