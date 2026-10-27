"""Django Ninja endpoints: Ninja's URL template and the real operation function are recorded,
not Ninja's per-path ``PathView.get_view`` closure."""

from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async

pytest.importorskip("ninja")

pytestmark = pytest.mark.django_db

API = "tests.testapp.ninja_api"


def test_route_template_and_view_are_ninjas(client, shop, latest):
    assert client.get("/api/orders/1").status_code == 200
    trace = latest()
    assert trace.framework == "django"
    assert trace.path == "/api/orders/1"
    assert trace.route == "/api/orders/<int:order_id>"
    assert trace.view == f"{API}.order_detail"
    assert "PathView" not in trace.view and "get_view" not in trace.view
    assert trace.status == 200 and trace.error is None


def test_plain_path_without_parameters(client, shop, latest):
    assert client.get("/api/orders").status_code == 200
    trace = latest()
    assert trace.route == "/api/orders"
    assert trace.view == f"{API}.list_orders"


def test_nested_router_template(client, shop, latest):
    assert client.get("/api/items/5").json() == {"item": 5}
    trace = latest()
    assert trace.route == "/api/items/<item_id>"
    assert trace.view == f"{API}.item_detail"


def test_view_is_picked_by_request_method(client, shop, latest):
    """One Django URL, several operations: the HTTP method selects the function."""
    assert client.get("/api/items/multi").json() == {"method": "GET"}
    assert latest().view == f"{API}.item_multi"
    assert client.put("/api/items/multi").json() == {"method": "PUT"}
    assert latest().view == f"{API}.item_multi"
    assert client.post("/api/items/multi").json() == {"method": "POST"}
    trace = latest()
    assert trace.route == "/api/items/multi"
    assert trace.view == f"{API}.item_multi_post"


def test_post_operation(client, shop, latest):
    assert client.post("/api/orders/3/touch").json() == {"touched": 3}
    trace = latest()
    assert trace.method == "POST"
    assert trace.route == "/api/orders/<int:order_id>/touch"
    assert trace.view == f"{API}.touch_order"


def test_unsupported_method_falls_back_without_crashing(client, shop, latest):
    # 405 from Ninja: no operation matches DELETE, so the wrapper name is all there is
    assert client.delete("/api/orders/1").status_code == 405
    trace = latest()
    assert trace.status == 405
    assert trace.route == "/api/orders/<int:order_id>"
    assert trace.view is not None and trace.error is None


def test_n_plus_one_gets_a_prefetch_suggestion(client, shop, latest):
    assert client.get("/api/orders").status_code == 200
    trace = latest()
    issues = [i for i in trace.issues if i.kind == "n_plus_one"]
    assert len(issues) == 1
    suggestion = issues[0].suggestion
    assert 'prefetch_related("items")' in suggestion.summary
    assert suggestion.confidence == "high"
    assert "--- a/ninja_api.py" in suggestion.diff
    assert "Order.objects.order_by" in suggestion.diff
    assert trace.query_count == 1 + len(shop["products"])  # 1 list + 6 orders' items


@pytest.mark.django_db(transaction=True)
async def test_async_endpoint(async_client, shop, latest):
    response = await async_client.get("/api/async/orders")
    assert response.status_code == 200
    assert len(response.json()["ids"]) == len(shop["products"])
    trace = await sync_to_async(latest)()
    assert trace.route == "/api/async/orders"
    assert trace.view == f"{API}.async_orders"
    assert trace.query_count == 1


def test_unhandled_exception_is_an_error(client, latest):
    with pytest.raises(RuntimeError, match="ninja kaboom"):
        client.get("/api/boom")
    trace = latest()
    assert trace.status == 500
    assert trace.error["type"] == "RuntimeError"
    assert trace.route == "/api/boom"
    assert trace.view == f"{API}.boom"


def test_ninja_http_error_is_a_client_error(client, latest):
    assert client.get("/api/gone").status_code == 404
    trace = latest()
    assert trace.status == 404 and trace.error is None
    assert trace.view == f"{API}.gone"


def test_view_helper_ignores_non_ninja_functions():
    from django_redline.middleware import _ninja_operation

    def plain(request): ...

    assert _ninja_operation(plain, "GET") is None
    assert _ninja_operation(object(), None) is None
