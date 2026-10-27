"""Deliberately inefficient views: each one is a case for the fix suggester."""

import time

from django.core.cache import cache
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import path
from django.views.generic import ListView

import redline_core

from .models import Comment, Customer, Order, OrderItem, Product, Profile, Tag


def reverse_fk(request):
    orders = Order.objects.order_by("id")
    data = []
    for order in orders:
        data.append([item.qty for item in order.items.all()])
    return JsonResponse({"data": data})


def forward_fk(request):
    items = OrderItem.objects.all()
    names = [item.product.name for item in items]
    return JsonResponse({"names": names})


def nested(request):
    orders = Order.objects.filter(status="new").prefetch_related("items")
    total = 0
    for order in orders:
        for item in order.items.all():
            total += item.product.price
    return JsonResponse({"total": total})


def m2m_forward(request):
    out = {}
    for product in Product.objects.all():
        out[product.name] = [tag.name for tag in product.tags.all()]
    return JsonResponse(out)


def m2m_reverse(request):
    tags = Tag.objects.all()[:50]
    out = {tag.name: [p.name for p in tag.products.all()] for tag in tags}
    return JsonResponse(out)


def reverse_o2o(request):
    customers = Customer.objects.all()
    bios = [customer.profile.bio for customer in customers]
    return JsonResponse({"bios": bios})


def forward_o2o(request):
    profiles = Profile.objects.all()
    names = [profile.customer.name for profile in profiles]
    return JsonResponse({"names": names})


def gfk(request):
    comments = Comment.objects.all()
    targets = [str(comment.content_object) for comment in comments]
    return JsonResponse({"targets": targets})


def generic_relation(request):
    products = Product.objects.all()
    counts = {p.name: len(p.comments.all()) for p in products}
    return JsonResponse(counts)


def filtered(request):
    orders = Order.objects.all()
    counts = [len(order.items.filter(qty__gt=1)) for order in orders]
    return JsonResponse({"counts": counts})


def _total(order):
    return sum(item.qty for item in order.items.all())


def not_found(request):
    orders = list_orders()
    return JsonResponse({"totals": [_total(o) for o in orders]})


def missing(request):
    from django.http import Http404

    raise Http404("no such order")


def list_orders():
    return list(Order.objects.all())


def template_loop(request):
    orders = Order.objects.all()
    return render(request, "testapp/orders.html", {"orders": orders})


class OrderListView(ListView):
    model = Order
    template_name = "testapp/orders.html"
    context_object_name = "orders"


def duplicate(request):
    first = Customer.objects.get(pk=request.GET.get("pk", 1))
    again = Customer.objects.get(pk=request.GET.get("pk", 1))
    return JsonResponse({"same": first.pk == again.pk})


def plain(request, pk=None):
    with redline_core.span("compute", kind="custom", answer=42):
        time.sleep(0.001)
    return JsonResponse({"ok": True, "password": "hunter2"})


def boom(request):
    raise ValueError("kaboom")


def streaming(request):
    def chunks():
        yield b"one,"
        yield b"two"

    return StreamingHttpResponse(chunks(), content_type="text/plain")


def file_view(request):
    return FileResponse(open(__file__, "rb"), content_type="text/plain")


def cached(request):
    value = cache.get("answer")
    if value is None:
        cache.set("answer", 42, 30)
        value = 42
    cache.get_many(["answer", "missing"])
    return JsonResponse({"value": value})


def templated(request):
    return render(request, "testapp/page.html", {"items": [1, 2, 3]})


async def async_view(request):
    names = [c.name async for c in Customer.objects.all()]
    value = await cache.aget("async-key")
    return JsonResponse({"names": names, "value": value})


async def async_n_plus_one(request):
    totals = []
    async for order in Order.objects.all():
        totals.append(len([i async for i in order.items.all()]))
    return JsonResponse({"totals": totals})


def html_page(request):
    response = HttpResponse(
        "<html><body><h1>Hi</h1></body></html>", content_type="text/html; charset=utf-8"
    )
    csp = request.GET.get("csp")
    if csp:
        response["Content-Security-Policy"] = csp
    if request.GET.get("timing"):
        response["Server-Timing"] = "app;dur=1"
    if request.GET.get("download"):
        response["Content-Disposition"] = 'attachment; filename="page.html"'
    if request.GET.get("length"):
        response["Content-Length"] = str(len(response.content))
    return response


def html_stream(request):
    return StreamingHttpResponse(iter([b"<html><body>x</body></html>"]), content_type="text/html")


def echo(request):
    return HttpResponse(request.body, content_type="application/json")


urlpatterns = [
    path("n1/reverse-fk/", reverse_fk),
    path("n1/forward-fk/", forward_fk),
    path("n1/nested/", nested),
    path("n1/m2m/", m2m_forward),
    path("n1/m2m-reverse/", m2m_reverse),
    path("n1/reverse-o2o/", reverse_o2o),
    path("n1/forward-o2o/", forward_o2o),
    path("n1/gfk/", gfk),
    path("n1/generic-relation/", generic_relation),
    path("n1/filtered/", filtered),
    path("n1/not-found/", not_found),
    path("n1/template/", template_loop),
    path("n1/cbv/", OrderListView.as_view()),
    path("n1/async/", async_n_plus_one),
    path("dup/", duplicate),
    path("plain/<int:pk>/", plain),
    path("plain/", plain),
    path("boom/", boom),
    path("missing/", missing),
    path("stream/", streaming),
    path("file/", file_view),
    path("cached/", cached),
    path("templated/", templated),
    path("async/", async_view),
    path("echo/", echo),
    path("html/", html_page),
    path("html-stream/", html_stream),
    path("static/thing.css", plain),
]
