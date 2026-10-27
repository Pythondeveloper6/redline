from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from django.test import override_settings

import redline_core.blocking as bl

pytestmark = pytest.mark.django_db


def kinds(trace, kind):
    return [s for s in trace.spans if s.kind == kind]


# ── event-loop blocking (async views) ─────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
async def test_async_view_with_time_sleep_is_blocking_in_async(async_client, latest):
    response = await async_client.get("/blocking/sleep/")
    assert response.status_code == 200
    trace = await sync_to_async(latest)()
    [span] = kinds(trace, "blocking")
    assert span.data["reason"] == "loop_lag" and span.data["call"] == "time.sleep"
    assert span.callsite is not None and span.callsite.file == "views_async_tx.py"
    assert span.callsite.code == "time.sleep(0.06)  # blocks the event loop"
    [issue] = [i for i in trace.issues if i.kind == "blocking_in_async"]
    assert issue.title.startswith("Event loop blocked ")
    assert issue.title.endswith(f"by time.sleep at views_async_tx.py:{span.callsite.line}")
    assert not bl._active and not bl._loop_threads


@pytest.mark.django_db(transaction=True)
async def test_sync_orm_on_the_event_loop_is_a_sync_call(async_client, latest, monkeypatch):
    monkeypatch.setenv("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
    response = await async_client.get("/blocking/sync-orm/")
    assert response.status_code == 200
    trace = await sync_to_async(latest)()
    [sql] = kinds(trace, "sql")
    blocking = kinds(trace, "blocking")
    assert [s.data["reason"] for s in blocking] == ["sync_call"]
    assert blocking[0].data["call"] == "sync ORM" and blocking[0].data["span_id"] == sql.id
    assert blocking[0].duration_ms >= 20
    [issue] = [i for i in trace.issues if i.kind == "blocking_in_async"]
    assert "by sync ORM" in issue.title
    assert "aget" in issue.suggestion.code


@pytest.mark.django_db(transaction=True)
async def test_async_orm_and_sync_views_under_asgi_do_not_block(async_client, shop, latest):
    await async_client.get("/async/")
    trace = await sync_to_async(latest)()
    assert kinds(trace, "blocking") == []
    await async_client.get("/plain/")  # a sync view runs in a worker thread
    trace = await sync_to_async(latest)()
    assert kinds(trace, "blocking") == []


def test_sync_requests_never_start_the_monitor(client, latest):
    client.get("/plain/")
    assert not bl._active
    assert kinds(latest(), "blocking") == []


# ── transactions ─────────────────────────────────────────────────────────────────────


def test_atomic_block_is_one_transaction_span(client, latest):
    client.get("/tx/commit/")
    trace = latest()
    [tx] = kinds(trace, "transaction")
    assert tx.data == {"db_alias": "default", "outcome": "commit", "queries": 2, "http_out": 1}
    assert tx.callsite is not None and tx.callsite.file == "views_async_tx.py"
    assert tx.callsite.code == "with transaction.atomic():"
    sql = kinds(trace, "sql")
    inserts = [s for s in sql if s.data["sql"].startswith("INSERT")]
    assert len(inserts) == 2 and all(s.data["in_transaction"] for s in inserts)
    assert not sql[-1].data.get("in_transaction")  # the count() after the block
    view = next(s for s in trace.spans if s.kind == "view")
    assert tx.parent_id == view.id
    [issue] = [i for i in trace.issues if i.kind == "external_in_transaction"]
    assert issue.title.startswith("HTTP call to api.stripe.com inside a transaction (")
    assert issue.title.endswith("views_async_tx.py:" + str(issue.callsite.line) + ")")
    assert "on_commit" in issue.suggestion.summary


def test_atomic_rollback_outcome(client, latest):
    client.get("/tx/rollback/")
    [tx] = kinds(latest(), "transaction")
    assert tx.data["outcome"] == "rollback" and tx.data["queries"] == 1


@override_settings(
    REDLINE={
        "STORAGE_PATH": ":memory:",
        "SAMPLE_RATE": 1.0,
        "PROJECT_ROOT": str(settings.BASE_DIR / "tests" / "testapp"),
        "LONG_TRANSACTION_MS": 10,
    }
)
def test_atomic_decorator_and_long_transaction(client, latest):
    client.get("/tx/decorated/")
    trace = latest()
    [tx] = kinds(trace, "transaction")
    assert tx.data["outcome"] == "commit" and tx.duration_ms >= 30
    [issue] = [i for i in trace.issues if i.kind == "long_transaction"]
    assert issue.severity == "warning"
    assert "on default" in issue.title


def test_atomic_requests_gives_one_transaction_per_request(client, latest, monkeypatch):
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    client.get("/tx/plain/")
    trace = latest()
    [tx] = kinds(trace, "transaction")
    assert tx.data["queries"] == 2 and tx.data["outcome"] == "commit"
    assert all(s.data.get("in_transaction") for s in kinds(trace, "sql"))
    client.get("/tx/commit/")  # the view's own atomic() nests inside the request's
    assert len(kinds(latest(), "transaction")) == 1


def test_no_transaction_span_without_atomic(client, latest):
    client.get("/tx/plain/")
    trace = latest()
    assert kinds(trace, "transaction") == []
    assert not any(s.data.get("in_transaction") for s in kinds(trace, "sql"))


def test_atomic_outside_requests_is_untouched(db):
    from django.db import transaction

    from tests.testapp.models import Tag

    with transaction.atomic():
        Tag.objects.create(name="x")
    assert getattr(connection, "_redline_tx", None) is None
