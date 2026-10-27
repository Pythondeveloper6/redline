"""Result-size, serializer and external-call detectors on synthetic traces."""

from __future__ import annotations

from redline_core import sql as sqlmod
from redline_core.conf import Settings
from redline_core.detect import Detector
from redline_core.detectors import REGISTRY, external, results, serializers
from redline_core.model import Callsite, Frame, Span, new_id
from tests.conftest import make_trace, sql_span


def cs(code: str, line: int = 10, file: str = "shop/views.py") -> Callsite:
    return Callsite(file, line, "view", code, [Frame(file, line, "view")])


def select(sql="SELECT id, body FROM shop_post", *, rows=1, code="posts = list(qs)", **kw):
    span = sql_span(sql, callsite=cs(code), **kw)
    span.data["rows"] = rows
    span.data["has_limit"] = sqlmod.has_limit(sql)
    return span


def http(method, url, status=200, ms=10.0, line=5):
    host = url.split("//", 1)[1].split("/", 1)[0]
    return Span(
        id=new_id(),
        parent_id=None,
        kind="http_out",
        name=f"{method} {host}",
        start_ms=0.0,
        duration_ms=ms,
        callsite=cs("requests.get(url)", line),
        data={"method": method, "url": url, "status": status},
    )


def ser(name="OrderSerializer", ms=80.0, items=100, library="drf"):
    return Span(
        id=new_id(),
        parent_id=None,
        kind="serializer",
        name=name,
        start_ms=0.0,
        duration_ms=ms,
        callsite=cs("return Response(s.data)"),
        data={"library": library, "serializer": name, "items": items, "many": True},
    )


def kinds(issues):
    return sorted(i.kind for i in issues)


def test_registered():
    for module in (results, serializers, external):
        assert module.detect in REGISTRY


# ── sql helpers ────────────────────────────────────────────────────────────────────


def test_has_limit_heuristic():
    assert sqlmod.has_limit('SELECT "a" FROM t LIMIT 21')
    assert sqlmod.has_limit("SELECT a FROM t ORDER BY a OFFSET 5 ROWS FETCH FIRST 10 ROWS ONLY")
    assert sqlmod.has_limit("SELECT TOP 5 a FROM t")
    assert not sqlmod.has_limit("SELECT a FROM t WHERE id IN (SELECT id FROM u LIMIT 5)")
    assert not sqlmod.has_limit("SELECT a FROM t WHERE note = 'LIMIT 5'")
    assert not sqlmod.has_limit("SELECT a FROM t")


def test_select_columns():
    sql = 'SELECT "p"."id", p.body AS p_body, count(*), "name" FROM shop_post p'
    assert sqlmod.select_columns(sql) == [("p", "id"), ("p", "body"), (None, "name")]
    assert sqlmod.select_columns("WITH x AS (SELECT 1) SELECT * FROM x") == []
    assert sqlmod.select_columns("SELECT t.* FROM t") == [("t", "*")]


# ── large_result / unbounded_query ──────────────────────────────────────────────────


def test_large_and_unbounded_fire_together():
    trace = make_trace(spans=[select(rows=1500)])
    issues = results.detect(trace, Settings())
    assert kinds(issues) == ["large_result", "unbounded_query"]
    large = next(i for i in issues if i.kind == "large_result")
    assert large.severity == "warning"
    assert "1,500 rows" in large.title and "shop_post" in large.title
    assert large.extra["rows"] == 1500 and large.extra["has_limit"] is False
    assert large.extra["table"] == "shop_post"
    assert large.callsite is not None


def test_critical_at_five_times_threshold():
    trace = make_trace(spans=[select(rows=5000), select("SELECT a FROM u", rows=1000)])
    by_kind = {(i.kind, i.extra["table"]): i for i in results.detect(trace, Settings())}
    assert by_kind["large_result", "shop_post"].severity == "critical"
    assert by_kind["unbounded_query", "shop_post"].severity == "critical"
    assert by_kind["unbounded_query", "u"].severity == "critical"  # 1000 >= 5 * 200
    assert by_kind["large_result", "u"].severity == "warning"


def test_limited_query_is_large_but_not_unbounded():
    trace = make_trace(spans=[select("SELECT a FROM t LIMIT 5000", rows=5000)])
    assert kinds(results.detect(trace, Settings())) == ["large_result"]


def test_below_thresholds_and_unknown_rows_are_quiet():
    quiet = [select(rows=199), select("SELECT a FROM t2", rows=None)]
    assert results.detect(make_trace(spans=quiet), Settings()) == []
    only_unbounded = results.detect(make_trace(spans=[select(rows=300)]), Settings())
    assert kinds(only_unbounded) == ["unbounded_query"]


def test_missing_has_limit_does_not_fire_unbounded():
    span = select(rows=900)
    del span.data["has_limit"]
    assert results.detect(make_trace(spans=[span]), Settings()) == []


def test_thresholds_follow_settings():
    trace = make_trace(spans=[select(rows=60)])
    s = Settings(unbounded_rows=50, large_result_rows=60)
    assert kinds(results.detect(trace, s)) == ["large_result", "unbounded_query"]


def test_same_query_same_line_is_one_issue_with_stable_id():
    a = results.detect(make_trace(spans=[select(rows=300), select(rows=400)]), Settings())
    b = results.detect(make_trace(spans=[select(rows=999)]), Settings())
    assert len(a) == 1 and a[0].count == 2 and a[0].extra["rows"] == 400
    assert a[0].id == b[0].id


def test_wide_columns_passthrough():
    span = select(rows=2000)
    span.data["wide_columns"] = ["body"]
    large = results.detect(make_trace(spans=[span]), Settings())[0]
    assert large.extra["wide_columns"] == ["body"]


# ── inefficient_count ────────────────────────────────────────────────────────────────


def test_len_of_full_select():
    span = select(code="n = len(Post.objects.filter(a=1))", rows=5)
    (issue,) = results.detect(make_trace(spans=[span]), Settings())
    assert issue.kind == "inefficient_count"
    assert issue.extra["pattern"] == "len"
    assert issue.extra["fix_line"] == "n = Post.objects.filter(a=1).count()"
    assert issue.severity == "info"
    assert ".count" in issue.detail


def test_truthy_and_bool():
    one = results.detect(make_trace(spans=[select(code="if posts:")]), Settings())[0]
    assert one.extra["fix_line"] == "if posts.exists():"
    two = results.detect(make_trace(spans=[select(code="if not Post.objects.all():")]), Settings())[
        0
    ]
    assert two.extra["fix_line"] == "if not Post.objects.all().exists():"
    three = results.detect(make_trace(spans=[select(code="return bool(posts)")]), Settings())[0]
    assert three.extra["pattern"] == "bool"
    assert three.extra["fix_line"] == "return posts.exists()"


def test_count_comparisons_need_a_count_query():
    count = "SELECT COUNT(*) AS c FROM shop_post"
    for code, fix in [
        ("if Post.objects.filter(a=1).count() > 0:", "if Post.objects.filter(a=1).exists():"),
        ("if qs.count() >= 1:", "if qs.exists():"),
        ("if qs.count() == 0:", "if not qs.exists():"),
    ]:
        span = select(count, code=code)
        (issue,) = results.detect(make_trace(spans=[span]), Settings())
        assert issue.extra["fix_line"] == fix
        assert issue.extra["pattern"] == "count_cmp"
    # a full-row SELECT on a `.count() > 0` line is not this pattern
    assert results.detect(make_trace(spans=[select(code="if qs.count() > 0:")]), Settings()) == []


def test_count_false_positives():
    for code in ["x = len([1, 2, 3])", "n = len(items)  # list", "if a == b:", "for p in qs:"]:
        # a literal never matches; a plain name only fires if a SELECT is issued on the line
        spans = [select(code=code)] if "items" not in code else []
        got = results.detect(make_trace(spans=spans), Settings())
        assert not [i for i in got if i.kind == "inefficient_count"], code
    bounded = select("SELECT a FROM t LIMIT 1", code="if Post.objects.filter(a=1):")
    assert results.detect(make_trace(spans=[bounded]), Settings()) == []
    exists = select("SELECT 1 AS a FROM t LIMIT 1", code="if qs.exists():")
    assert results.detect(make_trace(spans=[exists]), Settings()) == []


def test_heavy_full_load_is_a_warning():
    span = select(code="n = len(posts)", rows=500)
    (issue,) = [
        i
        for i in results.detect(make_trace(spans=[span]), Settings())
        if i.kind == "inefficient_count"
    ]
    assert issue.severity == "warning"


# ── slow_serializer ───────────────────────────────────────────────────────────────────


def test_slow_serializer():
    trace = make_trace(spans=[ser(ms=80), ser("Fast", ms=5)])
    (issue,) = serializers.detect(trace, Settings())
    assert issue.kind == "slow_serializer" and issue.severity == "warning"
    assert (
        "OrderSerializer" in issue.title and "80 ms" in issue.title and "100 items" in issue.title
    )
    assert issue.extra["library"] == "drf" and issue.extra["items"] == 100
    assert issue.extra["serializer"] == "OrderSerializer"
    crit = serializers.detect(make_trace(spans=[ser(ms=300)]), Settings())[0]
    assert crit.severity == "critical"
    assert serializers.detect(make_trace(spans=[ser(ms=30)]), Settings(slow_serializer_ms=20))


# ── external ─────────────────────────────────────────────────────────────────────────


def test_slow_external():
    trace = make_trace(
        spans=[http("GET", "https://api.x.io/v1/a?k=1", ms=700), http("GET", "https://b.io/", ms=3)]
    )
    (issue,) = external.detect(trace, Settings())
    assert issue.kind == "slow_external" and issue.severity == "warning"
    assert issue.extra["host"] == "api.x.io" and "700 ms" in issue.title
    crit = external.detect(make_trace(spans=[http("GET", "https://a.io/", ms=2600)]), Settings())
    assert crit[0].severity == "critical"


def test_duplicate_external():
    spans = [
        http("GET", "https://a.io/u/1"),
        http("GET", "https://a.io/u/1"),
        http("GET", "https://a.io/u/2"),
    ]
    (issue,) = external.detect(make_trace(spans=spans), Settings())
    assert issue.kind == "duplicate_external" and issue.count == 2 and issue.severity == "info"
    assert issue.extra["url"] == "https://a.io/u/1"
    five = [http("GET", "https://a.io/u/1") for _ in range(5)]
    assert external.detect(make_trace(spans=five), Settings())[0].severity == "warning"
    # method matters
    mixed = [http("GET", "https://a.io/u/1"), http("POST", "https://a.io/u/1")]
    assert external.detect(make_trace(spans=mixed), Settings()) == []


def test_retry_storm_replaces_duplicate():
    spans = [
        http("POST", "https://pay.io/charge", status=503),
        http("POST", "https://pay.io/charge", status=429),
        http("POST", "https://pay.io/charge", status=200),
    ]
    issues = external.detect(make_trace(spans=spans), Settings())
    assert kinds(issues) == ["retry_storm"]
    assert issues[0].extra["failed"] == 2 and issues[0].extra["host"] == "pay.io"
    assert issues[0].severity == "warning"
    big = [http("POST", f"https://pay.io/c/{i}", status=None) for i in range(6)]
    assert external.detect(make_trace(spans=big), Settings())[0].severity == "critical"


def test_no_retry_storm_without_failure_or_enough_calls():
    ok = [http("GET", f"https://a.io/{i}") for i in range(4)]
    assert external.detect(make_trace(spans=ok), Settings()) == []
    two = [http("GET", "https://a.io/x", status=500), http("GET", "https://a.io/y")]
    assert external.detect(make_trace(spans=two), Settings()) == []


# ── through the Detector ─────────────────────────────────────────────────────────────


def test_detector_runs_registry_and_generic_suggestions():
    spans = [select(rows=2000), ser(ms=60), http("GET", "https://a.io/", ms=900)]
    issues = Detector(Settings()).detect(make_trace(spans=spans))
    assert {"large_result", "unbounded_query", "slow_serializer", "slow_external"} <= {
        i.kind for i in issues
    }
    assert all(i.suggestion is not None for i in issues)
    assert all(i.to_dict()["extra"] is not None for i in issues)
