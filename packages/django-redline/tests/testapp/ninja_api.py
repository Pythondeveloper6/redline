"""A small Django Ninja API: the route template and view must be Ninja's, not its internals."""

from ninja import NinjaAPI, Router

from .models import Order

api = NinjaAPI(urls_namespace="shop_api", docs=None)


@api.get("/orders")
def list_orders(request):
    """N+1: the per-order ``items`` lookup should become a ``prefetch_related``."""
    orders = Order.objects.order_by("id")
    data = []
    for order in orders:
        data.append({"id": order.id, "quantities": [item.qty for item in order.items.all()]})
    return data


@api.get("/orders/{int:order_id}")
def order_detail(request, order_id: int):
    order = Order.objects.get(pk=order_id)
    return {"id": order.id, "status": order.status}


@api.post("/orders/{int:order_id}/touch")
def touch_order(request, order_id: int):
    return {"touched": order_id}


@api.get("/async/orders")
async def async_orders(request):
    ids = [order.id async for order in Order.objects.order_by("id")]
    return {"ids": ids}


@api.get("/boom")
def boom(request):
    raise RuntimeError("ninja kaboom")


@api.get("/gone")
def gone(request):
    from ninja.errors import HttpError

    raise HttpError(404, "no such thing")


items = Router()


@items.api_operation(["GET", "PUT"], "/multi")
def item_multi(request):
    return {"method": request.method}


@items.post("/multi")
def item_multi_post(request):
    return {"method": request.method}


@items.get("/{item_id}")
def item_detail(request, item_id: int):
    return {"item": item_id}


api.add_router("/items", items)
