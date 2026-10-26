"""End-to-end: a real request on real models → N+1 issue → the right fix and diff."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


def _n1(trace):
    issues = [i for i in trace.issues if i.kind == "n_plus_one"]
    assert issues, [i.title for i in trace.issues]
    return issues


def _get(client, latest, url):
    response = client.get(url)
    assert response.status_code == 200, response.content
    return latest()


def test_reverse_fk_prefetch_diff(client, shop, latest):
    trace = _get(client, latest, "/n1/reverse-fk/")
    issue = _n1(trace)[0]
    s = issue.suggestion
    assert s.confidence == "high"
    assert 'prefetch_related("items")' in s.summary
    assert s.code == 'orders = Order.objects.prefetch_related("items").order_by("id")'
    assert '-    orders = Order.objects.order_by("id")' in s.diff
    assert '+    orders = Order.objects.prefetch_related("items").order_by("id")' in s.diff
    assert s.diff.startswith("--- a/views.py\n+++ b/views.py")
    assert s.docs_url.endswith("#prefetch-related")


def test_forward_fk_select_related_replaces_all(client, shop, latest):
    issue = _n1(_get(client, latest, "/n1/forward-fk/"))[0]
    s = issue.suggestion
    assert s.code == 'items = OrderItem.objects.select_related("product")'
    assert s.confidence == "high"
    assert s.docs_url.endswith("#select-related")


def test_nested_lookup_extends_existing_prefetch(client, shop, latest):
    issue = _n1(_get(client, latest, "/n1/nested/"))[0]
    s = issue.suggestion
    assert "items__product" in s.summary
    assert s.code == (
        'orders = Order.objects.filter(status="new").prefetch_related("items", "items__product")'
    )
    assert s.diff


def test_m2m_forward(client, shop, latest):
    s = _n1(_get(client, latest, "/n1/m2m/"))[0].suggestion
    assert 'prefetch_related("tags")' in s.summary
    assert '+    for product in Product.objects.prefetch_related("tags"):' in s.diff


def test_m2m_reverse_inserts_before_slice(client, shop, latest):
    s = _n1(_get(client, latest, "/n1/m2m-reverse/"))[0].suggestion
    assert s.code == 'tags = Tag.objects.prefetch_related("products")[:50]'


def test_reverse_one_to_one_uses_select_related(client, shop, latest):
    s = _n1(_get(client, latest, "/n1/reverse-o2o/"))[0].suggestion
    assert s.code == 'customers = Customer.objects.select_related("profile")'


def test_forward_one_to_one(client, shop, latest):
    s = _n1(_get(client, latest, "/n1/forward-o2o/"))[0].suggestion
    assert s.code == 'profiles = Profile.objects.select_related("customer")'


def test_generic_foreign_key_low_confidence(client, shop, latest):
    s = _n1(_get(client, latest, "/n1/gfk/"))[0].suggestion
    assert s.confidence == "low"
    assert "GenericForeignKey" in s.summary
    assert s.code == 'comments = Comment.objects.prefetch_related("content_object")'


def test_generic_relation(client, shop, latest):
    s = _n1(_get(client, latest, "/n1/generic-relation/"))[0].suggestion
    assert s.code == 'products = Product.objects.prefetch_related("comments")'


def test_filtered_relation_suggests_prefetch_object(client, shop, latest):
    s = _n1(_get(client, latest, "/n1/filtered/"))[0].suggestion
    assert "Prefetch(" in s.code
    assert s.diff is None
    assert s.confidence == "medium"


def test_queryset_not_found_gives_code_hint(client, shop, latest):
    s = _n1(_get(client, latest, "/n1/not-found/"))[0].suggestion
    assert s.diff is None
    assert s.code == 'Order.objects.prefetch_related("items")'
    assert s.confidence == "medium"


def test_template_loop_found_from_render_line(client, shop, latest):
    s = _n1(_get(client, latest, "/n1/template/"))[0].suggestion
    assert s.code == 'orders = Order.objects.prefetch_related("items")'


def test_class_based_view_gets_queryset_attribute(client, shop, latest):
    trace = _get(client, latest, "/n1/cbv/")
    issue = _n1(trace)[0]
    # the query comes from the template, attributed to the view class
    assert issue.callsite.function == "OrderListView"
    s = issue.suggestion
    assert s.code == 'queryset = Order.objects.prefetch_related("items")'
    assert '+    queryset = Order.objects.prefetch_related("items")' in s.diff
    assert s.confidence == "medium"


@pytest.mark.django_db(transaction=True)
async def test_async_view_n_plus_one(async_client, shop, latest):
    response = await async_client.get("/n1/async/")
    assert response.status_code == 200
    from asgiref.sync import sync_to_async

    trace = await sync_to_async(latest)()
    issue = _n1(trace)[0]
    assert 'prefetch_related("items")' in issue.suggestion.summary


def test_duplicate_query_falls_back_to_generic(client, shop, latest):
    trace = _get(client, latest, "/dup/")
    dup = [i for i in trace.issues if i.kind == "duplicate_query"]
    assert dup and dup[0].count == 2
    assert dup[0].suggestion.confidence == "low"


def _issue_for(sql: str, frames: list[tuple[str, int, str]], code: str):
    """An N+1 issue on ``sql`` whose callsite stack is ``frames`` (outermost first)."""
    from redline_core import sql as sqlmod
    from redline_core.model import Callsite, Frame, Issue, Span

    stack = tuple(Frame(file=f, line=ln, function=fn) for f, ln, fn in frames)
    inner = stack[-1]
    callsite = Callsite(inner.file, inner.line, inner.function, code, stack)
    span = Span(
        id="s1",
        parent_id=None,
        kind="sql",
        name="q",
        start_ms=0,
        duration_ms=1,
        callsite=callsite,
        data={"sql": sql, "params": [1], "fingerprint": sqlmod.fingerprint(sql)},
    )
    issue = Issue(
        id="i",
        kind="n_plus_one",
        severity="warning",
        title="t",
        detail="d",
        span_ids=["s1"],
        fingerprint=span.data["fingerprint"],
        callsite=callsite,
        count=6,
        total_ms=6,
    )
    return issue, [span]


def _line_of(text: str, path: str = "serializers.py") -> int:
    from pathlib import Path

    lines = (Path(__file__).parent / "testapp" / path).read_text().splitlines()
    return next(i for i, line in enumerate(lines, 1) if text in line)


def test_serializer_callsite_targets_the_views_get_queryset(rt):
    from django_redline.suggest import DjangoFixSuggester

    sql = (
        'SELECT "testapp_orderitem"."id", "testapp_orderitem"."order_id" FROM '
        '"testapp_orderitem" WHERE "testapp_orderitem"."order_id" = %s'
    )
    view_line = _line_of("class OrderViewSet")
    method_line = _line_of("return len(obj.items.all())")
    issue, spans = _issue_for(
        sql,
        [
            ("serializers.py", view_line, "OrderViewSet"),
            ("serializers.py", method_line, "item_count"),
        ],
        "return len(obj.items.all())",
    )
    s = DjangoFixSuggester(rt.settings.project_root).suggest(issue, spans)
    assert "OrderSerializer" in s.summary and "get_queryset()" in s.summary
    assert s.code == 'return Order.objects.filter(status="new").prefetch_related("items")'
    assert s.diff and s.confidence == "high"


def test_serializer_without_view_gives_hint(rt):
    from django_redline.suggest import DjangoFixSuggester

    sql = 'SELECT * FROM "testapp_orderitem" WHERE "testapp_orderitem"."order_id" = %s'
    method_line = _line_of("return len(obj.items.all())")
    issue, spans = _issue_for(
        sql, [("serializers.py", method_line, "item_count")], "return len(obj.items.all())"
    )
    s = DjangoFixSuggester(rt.settings.project_root).suggest(issue, spans)
    assert s.diff is None
    assert "the view's get_queryset()" in s.summary
    assert s.code == 'Order.objects.prefetch_related("items")'


def test_unknown_table_declines(rt):
    from django_redline.suggest import DjangoFixSuggester

    issue, spans = _issue_for(
        'SELECT * FROM "nope" WHERE "nope"."x_id" = %s', [("x.py", 1, "f")], ""
    )
    assert DjangoFixSuggester(rt.settings.project_root).suggest(issue, spans) is None


def test_ambiguous_foreign_keys_resolved_by_source_line(rt):
    from django_redline.suggest import DjangoFixSuggester

    # Customer is the target of Order.customer and Profile.customer: the code decides.
    sql = 'SELECT * FROM "testapp_customer" WHERE "testapp_customer"."id" = %s LIMIT 21'
    line = _line_of("names = [profile.customer.name", "views.py")
    issue, spans = _issue_for(
        sql,
        [("views.py", line, "forward_o2o")],
        "names = [profile.customer.name for profile in profiles]",
    )
    s = DjangoFixSuggester(rt.settings.project_root).suggest(issue, spans)
    assert s.code == 'profiles = Profile.objects.select_related("customer")'
