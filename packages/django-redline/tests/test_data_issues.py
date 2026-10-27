"""Result-size and count issues end to end: real requests, real sqlite, real suggestions."""

from __future__ import annotations

import pytest

from tests.testapp.models import Article

pytestmark = pytest.mark.django_db


@pytest.fixture
def articles(db):
    Article.objects.bulk_create(
        [Article(title=f"a{i}", body="x" * 50, meta={"i": i}) for i in range(1100)]
    )


def _get(client, latest, url):
    assert client.get(url).status_code == 200
    return latest()


def _issue(trace, kind):
    found = [i for i in trace.issues if i.kind == kind]
    assert found, [i.title for i in trace.issues]
    return found[0]


def _selects(trace):
    return [s for s in trace.spans if s.kind == "sql" and s.data["sql"].startswith("SELECT")]


def test_rows_and_has_limit_are_recorded_on_sqlite(client, articles, latest):
    trace = _get(client, latest, "/data/big/")
    span = _selects(trace)[-1]
    assert span.data["rows"] == 1100  # sqlite rowcount is -1: counted from the fetches
    assert span.data["has_limit"] is False
    trace = _get(client, latest, "/data/limited/")
    span = _selects(trace)[-1]
    assert span.data["rows"] == 300 and span.data["has_limit"] is True


def test_rows_zero_and_single(client, articles, latest):
    trace = _get(client, latest, "/data/fine/")
    first, exists = _selects(trace)[-2:]
    assert first.data["rows"] == 1 and first.data["has_limit"] is True
    assert exists.data["rows"] == 1


def test_unbounded_and_large_with_wide_columns_defer_diff(client, articles, latest):
    trace = _get(client, latest, "/data/big/")
    large, unbounded = _issue(trace, "large_result"), _issue(trace, "unbounded_query")
    assert large.extra["rows"] == 1100
    assert large.severity == "warning"
    assert unbounded.severity == "critical"  # 1100 >= 5 * 200
    assert unbounded.extra["wide_columns"] == ["body", "meta", "cover"]
    s = unbounded.suggestion
    assert s.code == 'articles = Article.objects.defer("body", "meta", "cover").order_by("id")'
    assert '-    articles = Article.objects.order_by("id")' in s.diff
    assert '+    articles = Article.objects.defer("body", "meta", "cover").order_by("id")' in s.diff
    assert ".only()" in s.summary and "Paginate" in s.summary
    assert s.confidence == "high"
    assert large.suggestion.diff == s.diff


def test_limited_query_only_large_when_over_threshold(client, articles, latest, rt):
    trace = _get(client, latest, "/data/limited/")
    assert not [i for i in trace.issues if i.kind in ("large_result", "unbounded_query")]


def test_len_gets_count_diff(client, articles, latest):
    trace = _get(client, latest, "/data/len/")
    issue = _issue(trace, "inefficient_count")
    assert issue.extra["pattern"] == "len"
    s = issue.suggestion
    assert s.code == "n = Article.objects.filter(views=0).count()"
    assert "-    n = len(Article.objects.filter(views=0))" in s.diff
    assert "+    n = Article.objects.filter(views=0).count()" in s.diff
    assert issue.severity == "warning"  # loaded 1100 rows
    assert s.confidence == "high"


def test_truthy_queryset_gets_exists(client, articles, latest):
    trace = _get(client, latest, "/data/truthy/")
    issue = _issue(trace, "inefficient_count")
    s = issue.suggestion
    assert s.code == "if articles.exists():"
    assert "-    if articles:" in s.diff and "+    if articles.exists():" in s.diff


def test_count_gt_zero_gets_exists(client, articles, latest):
    trace = _get(client, latest, "/data/count-gt/")
    issue = _issue(trace, "inefficient_count")
    assert issue.extra["pattern"] == "count_cmp"
    s = issue.suggestion
    assert s.code == "if Article.objects.filter(views=0).exists():"
    assert "+    if Article.objects.filter(views=0).exists():" in s.diff


def test_first_and_exists_are_clean(client, articles, latest):
    trace = _get(client, latest, "/data/fine/")
    assert not [i for i in trace.issues if i.kind == "inefficient_count"]


def test_iterator_diff_when_no_wide_columns(rt):
    from django_redline.suggest import DjangoFixSuggester
    from tests.test_suggest import _issue_for, _line_of

    sql = 'SELECT "testapp_customer"."id", "testapp_customer"."name" FROM "testapp_customer"'
    line = _line_of("customers = Customer.objects.all()", "views.py")
    issue, spans = _issue_for(
        sql, [("views.py", line + 1, "reverse_o2o")], "bios = [customer.profile.bio ...]"
    )
    issue.kind = "unbounded_query"
    issue.extra = {"rows": 500}
    s = DjangoFixSuggester(rt.settings.project_root).suggest(issue, spans)
    assert s.code == "customers = Customer.objects.all().iterator()"
    assert "[:100]" in s.summary
    assert s.diff and s.confidence == "medium"


def test_wide_columns_helper(rt):
    from django_redline.suggest import DjangoFixSuggester

    sql = (
        'SELECT "testapp_article"."id", "testapp_article"."title", "testapp_article"."body" '
        'FROM "testapp_article"'
    )
    model, wide = DjangoFixSuggester(rt.settings.project_root).wide_columns(sql)
    assert model is Article and wide == ["body"]
    only = 'SELECT "testapp_article"."id" FROM "testapp_article"'
    assert DjangoFixSuggester(rt.settings.project_root).wide_columns(only)[1] == []


def test_reused_cursor_counts_rows_per_execute_without_stacking(rt):
    """One cursor, many executes: each span counts its own rows and the fetch methods are
    decorated once (they used to stack a layer per execute → RecursionError at ~1000)."""
    from django.db import connection

    from django_redline.instrument import install_connection
    from redline_core.conf import Settings
    from redline_core.recorder import Recorder

    install_connection(connection)
    rec = Recorder(Settings.from_mapping({"max_spans_per_trace": 0}))
    with connection.cursor() as cursor:
        with rec.recording(method="GET", path="/x"):
            for _ in range(1200):
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)
            cursor.execute("SELECT 1 UNION ALL SELECT 2")
            assert cursor.fetchall() == [(1,), (2,)]
        spans = [s for s in rec.last_trace.spans if s.kind == "sql"]
        assert len(spans) == 1201
        assert [s.data["rows"] for s in spans[:-1]] == [1] * 1200
        assert spans[-1].data["rows"] == 2
        # outside a trace the reused cursor works and no longer counts into the old span
        cursor.execute("SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3")
        assert len(cursor.fetchall()) == 3
        assert spans[-1].data["rows"] == 2
