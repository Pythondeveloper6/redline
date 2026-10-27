"""Views with big results and inefficient counts."""

from django.http import JsonResponse
from django.urls import path

from .models import Article


def big_list(request):
    articles = Article.objects.order_by("id")
    return JsonResponse({"titles": [a.title for a in articles]})


def big_limited(request):
    articles = Article.objects.order_by("id")[:300]
    return JsonResponse({"titles": [a.title for a in articles]})


def count_len(request):
    n = len(Article.objects.filter(views=0))
    return JsonResponse({"n": n})


def truthy(request):
    articles = Article.objects.filter(views=0)
    if articles:
        return JsonResponse({"any": True})
    return JsonResponse({"any": False})


def count_gt(request):
    if Article.objects.filter(views=0).count() > 0:
        return JsonResponse({"any": True})
    return JsonResponse({"any": False})


def fine(request):
    first = Article.objects.filter(views=0).first()
    ok = Article.objects.filter(views=0).exists()
    return JsonResponse({"first": first.pk if first else None, "ok": ok})


urlpatterns = [
    path("data/big/", big_list),
    path("data/limited/", big_limited),
    path("data/len/", count_len),
    path("data/truthy/", truthy),
    path("data/count-gt/", count_gt),
    path("data/fine/", fine),
]
