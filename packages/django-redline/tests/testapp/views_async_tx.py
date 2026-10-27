"""Views for event-loop blocking and transaction detection."""

import asyncio
import time

from django.db import connection, transaction
from django.http import JsonResponse
from django.urls import path

from redline_core import record_span

from .models import Tag

SLOW_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 400000) "
    "SELECT count(*) FROM c"
)


async def async_sleep(request):
    await asyncio.sleep(0.005)
    time.sleep(0.06)  # blocks the event loop
    await asyncio.sleep(0.005)
    return JsonResponse({"ok": True})


async def async_sync_orm(request):
    # The wrong path: a sync database call straight from a coroutine (only possible
    # with DJANGO_ALLOW_ASYNC_UNSAFE).
    with connection.cursor() as cursor:
        cursor.execute(SLOW_SQL)
        (n,) = cursor.fetchone()
    await asyncio.sleep(0)
    return JsonResponse({"n": n})


def fake_http_call(url="https://api.stripe.com/v1/charges"):
    record_span(
        "http_out",
        "POST " + url.split("://", 1)[1],
        time.perf_counter(),
        0.003,
        {"method": "POST", "url": url, "status": 200},
    )


def atomic_commit(request):
    with transaction.atomic():
        Tag.objects.create(name="a")
        with transaction.atomic():  # a savepoint: no span of its own
            Tag.objects.create(name="b")
        fake_http_call()
    Tag.objects.count()  # after the transaction
    return JsonResponse({"ok": True})


def atomic_rollback(request):
    try:
        with transaction.atomic():
            Tag.objects.create(name="a")
            raise ValueError("nope")
    except ValueError:
        pass
    return JsonResponse({"ok": True})


@transaction.atomic
def atomic_decorated(request):
    Tag.objects.create(name="a")
    time.sleep(0.03)
    return JsonResponse({"ok": True})


def plain_writes(request):
    Tag.objects.create(name="a")
    Tag.objects.create(name="b")
    return JsonResponse({"ok": True})


urlpatterns = [
    path("blocking/sleep/", async_sleep),
    path("blocking/sync-orm/", async_sync_orm),
    path("tx/commit/", atomic_commit),
    path("tx/rollback/", atomic_rollback),
    path("tx/decorated/", atomic_decorated),
    path("tx/plain/", plain_writes),
]
