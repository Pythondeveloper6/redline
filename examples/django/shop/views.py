"""The demo shop's endpoints. Most of them are slow on purpose – Redline shows why."""

import os
import time
import uuid

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, F, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from django_redline import span

from .models import Customer, Order, OrderItem, Product, Review

# SHOP_REFACTOR=1 simulates a refactor for `redline check` (see the README):
# N+1 fixed, one view slower, one response field changed, one view broken.
REFACTOR = os.environ.get("SHOP_REFACTOR") == "1"

ENDPOINTS = [
    ("/patterns/", "aggregate scans, pagination, ORM instances and response rules"),
    ("/orders/", "N+1 on order items – fix: prefetch_related"),
    ("/items/", "N+1 on each item's product – fix: select_related"),
    ("/orders/page/", "N+1 inside a template loop"),
    ("/dashboard/", "the same query twice"),
    ("/reviews/search/?q=battery", "slow query on an unindexed column (EXPLAIN shows a scan)"),
    ("/slow/", "a slow view (time.sleep) – try /slow/?_redline=profile for a flamegraph"),
    ("/boom/", "an unhandled exception (500)"),
    ("/api/products/", "a JSON API (send an Authorization header – Redline redacts it)"),
    ("/async/summary/", "an async view"),
    ("/cached/", "a cached view (cache spans: miss, then hit)"),
]


def home(request):
    return render(request, "shop/home.html", {"endpoints": ENDPOINTS})


def order_list(request):
    """N+1: one query for the orders, then one per order for its items."""
    orders = Order.objects.select_related("customer").order_by("-id")[:30]
    if REFACTOR:  # refactor: two queries instead of one per order
        orders = orders.prefetch_related("items")
    data = []
    for order in orders:
        items = [{"product_id": i.product_id, "qty": i.quantity} for i in order.items.all()]
        data.append({"id": order.id, "customer": order.customer.name, "items": items})
    return JsonResponse({"orders": data})


def item_list(request):
    """N+1: every item loads its product with a separate query."""
    items = OrderItem.objects.order_by("-id")[:40]
    rows = []
    for item in items:
        rows.append({"id": item.id, "product": item.product.name, "qty": item.quantity})
    return JsonResponse({"items": rows})


def order_page(request):
    """N+1 in the template: {% for item in order.items.all %}."""
    orders = Order.objects.order_by("-id")[:20]
    return render(request, "shop/orders.html", {"orders": orders})


def _customer_count():
    return Customer.objects.count()


def dashboard(request):
    """A duplicate query: the same count runs twice."""
    if REFACTOR:  # refactor: a slower code path
        time.sleep(0.05)
    stats = {"customers": _customer_count(), "products": Product.objects.count()}
    stats["customers_again"] = _customer_count()
    return JsonResponse(stats)


def review_search(request):
    """Slow: LIKE on an unindexed text column of a big table, sorted by an unindexed column."""
    q = request.GET.get("q", "battery")
    reviews = Review.objects.filter(body__contains=q).order_by("-helpful")[:10]
    return JsonResponse({"reviews": [{"id": r.id, "rating": r.rating} for r in reviews]})


def slow(request):
    """A slow view with no database at all – profile it with ?_redline=profile."""
    with span("pricing.recalculate"):
        total = 0
        for i in range(200_000):
            total += i % 7
    time.sleep(0.35)
    return JsonResponse({"total": total})


def boom(request):
    raise RuntimeError("Payment provider timed out")


def api_products(request):
    """A JSON API endpoint; pretends to check a bearer token."""
    token = request.headers.get("Authorization", "")
    products = Product.objects.select_related("category").order_by("id")[:20]
    return JsonResponse(
        {
            "authenticated": token.startswith("Bearer "),
            "products": [
                {
                    "id": p.id,
                    "name": p.name,
                    "price": f"{p.price:.1f}" if REFACTOR else str(p.price),
                    "category": p.category.name,
                }
                for p in products
            ],
        }
    )


async def async_summary(request):
    """An async view using the async ORM."""
    if REFACTOR:  # refactor: a regression that breaks the view
        raise RuntimeError("summary backend not configured")
    orders = await Order.objects.acount()
    latest = [o.id async for o in Order.objects.order_by("-id")[:5]]
    top = await sync_to_async(lambda: list(Product.objects.order_by("-price")[:3]))()
    return JsonResponse({"orders": orders, "latest": latest, "top": [p.name for p in top]})


def cached(request):
    """Cache spans: the first request misses and fills the cache, the next ones hit."""
    data = cache.get("shop:top-products")
    if data is None:
        data = list(Product.objects.order_by("-price").values_list("name", flat=True)[:5])
        cache.set("shop:top-products", data, 60)
    return JsonResponse({"top": data})


def status(request):
    """A health check with volatile fields (change on every call)."""
    return JsonResponse({"ok": True, "now": time.time(), "request_id": uuid.uuid4().hex})


def patterns(request):
    """Compare aggregate scans, deep pagination and model construction."""
    qs = Product.objects.filter(price__gte=0)
    if REFACTOR:
        aggregates = qs.aggregate(count=Count("id"), total=Sum("price"))
        rows = list(Product.objects.order_by("id").values("id", "name", "price")[:30])
        items = [
            {"id": row["id"], "name": row["name"], "price": float(row["price"])} for row in rows
        ]
        items.reverse()
    else:
        aggregates = {"count": qs.count(), "total": qs.aggregate(total=Sum("price"))["total"]}
        list(Product.objects.order_by("id").values_list("id", flat=True)[10000:10005])
        rows = list(Product.objects.order_by("id")[:30])
        items = [{"id": row.id, "name": row.name, "price": float(row.price)} for row in rows]
    return JsonResponse(
        {
            "items": items,
            "count": aggregates["count"],
            "total": float(aggregates["total"] or 0) + (0.005 if REFACTOR else 0),
        }
    )


@require_POST
def pattern_writes(request):
    """Execute six individual writes and roll back; do not commit demo changes."""
    with transaction.atomic():
        ids = list(Product.objects.order_by("id").values_list("id", flat=True)[:6])
        for ident in ids:
            Product.objects.filter(pk=ident).update(price=F("price"))
        transaction.set_rollback(True)
    return JsonResponse({"writes": len(ids), "rolled_back": True})
