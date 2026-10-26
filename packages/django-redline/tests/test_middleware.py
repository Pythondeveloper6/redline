from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async
from django.test import override_settings

pytestmark = pytest.mark.django_db


def test_records_route_view_spans_and_redacts(client, latest):
    response = client.get(
        "/plain/7/?q=1&token=abc",
        HTTP_AUTHORIZATION="Bearer secret",
        HTTP_X_REDLINE_RUN="run42;step=3",
    )
    assert response.status_code == 200
    trace = latest()
    assert trace.framework == "django"
    assert trace.method == "GET"
    assert trace.path == "/plain/7/"
    assert trace.route == "/plain/<int:pk>/"
    assert trace.view == "tests.testapp.views.plain"
    assert trace.status == 200
    assert trace.tags["run_id"] == "run42" and trace.tags["step"] == "3"
    assert trace.request["headers"]["Authorization"] != "Bearer secret"
    assert trace.request["query"]["q"] == "1"
    assert trace.request["query"]["token"] != "abc"
    assert "hunter2" not in (trace.response["body"] or "")
    kinds = {s.kind for s in trace.spans}
    assert {"middleware", "view", "custom"} <= kinds
    view = next(s for s in trace.spans if s.kind == "view")
    custom = next(s for s in trace.spans if s.kind == "custom")
    assert custom.parent_id == view.id
    assert custom.data["answer"] == 42


def test_sql_spans_have_callsite_alias_vendor_rows(client, shop, latest):
    client.get("/n1/forward-fk/")
    trace = latest()
    sql = [s for s in trace.spans if s.kind == "sql"]
    assert len(sql) == trace.query_count >= 13
    first = sql[0]
    assert first.data["db_alias"] == "default"
    assert first.data["vendor"] == "sqlite"
    assert first.data["explainable"] is True
    assert first.callsite.file == "views.py"
    view = next(s for s in trace.spans if s.kind == "view")
    assert all(s.parent_id == view.id for s in sql)


def test_request_body_captured_and_redacted(client, latest):
    client.post("/echo/", data='{"user": "a", "password": "p"}', content_type="application/json")
    trace = latest()
    assert '"user"' in trace.request["body"]
    assert '"p"' not in trace.request["body"]


def test_exception_recorded_and_reraised(client, latest):
    with pytest.raises(ValueError):
        client.get("/boom/")
    trace = latest()
    assert trace.status == 500
    assert trace.error["type"] == "ValueError"
    assert "kaboom" in trace.error["message"]


def test_exception_without_raise_request_exception(client, latest):
    client.raise_request_exception = False
    response = client.get("/boom/")
    assert response.status_code == 500
    assert latest().error["type"] == "ValueError"


def test_streaming_response_not_consumed(client, latest):
    response = client.get("/stream/")
    assert b"".join(response.streaming_content) == b"one,two"
    trace = latest()
    assert trace.status == 200
    assert trace.response["body"] is None


def test_file_response_not_consumed(client, latest):
    response = client.get("/file/")
    body = b"".join(response.streaming_content)
    response.close()
    assert b"Deliberately inefficient views" in body
    assert latest().response["body"] is None


def test_ignored_paths_and_own_mount(client, staff_client, latest, rt):
    client.get("/static/thing.css")
    staff_client.get("/redline/api/meta")
    staff_client.get("/redline/")
    assert latest() is None


@override_settings(
    REDLINE={"STORAGE_PATH": ":memory:", "SAMPLE_RATE": 0.0, "ALWAYS_KEEP_SLOWER_THAN_MS": 1e9}
)
def test_sampling_drops_but_keeps_errors(client, latest):
    client.get("/plain/")
    assert latest() is None
    client.raise_request_exception = False
    client.get("/boom/")
    trace = latest()
    assert trace.status == 500 and trace.tags["sampled_by"] == "error"


@override_settings(REDLINE={"STORAGE_PATH": ":memory:", "ENABLED": False})
def test_disabled(client, latest):
    client.get("/plain/")
    assert latest() is None


def test_profile_opt_in(client, latest, rt):
    client.get("/plain/?_redline=profile")
    trace = latest()
    assert rt.store.has_profile(trace.id)
    profile = rt.store.get_profile(trace.id)
    assert profile["profiles"]


def test_profile_header(client, latest, rt):
    client.get("/plain/", HTTP_X_REDLINE_PROFILE="1")
    assert rt.store.has_profile(latest().id)


def test_cache_spans(client, latest):
    from django.core.cache import cache

    cache.clear()
    client.get("/cached/")
    spans = [s for s in latest().spans if s.kind == "cache"]
    ops = [(s.data["op"], s.data["key"], s.data["hit"]) for s in spans]
    assert ops[0] == ("get", "answer", False)
    assert ops[1][0] == "set"
    assert ops[2] == ("get_many", "answer, missing", False)
    assert spans[0].callsite.file == "views.py"
    assert spans[0].data["backend"].startswith("default (LocMemCache)")
    client.get("/cached/")
    assert next(s.data["hit"] for s in latest().spans if s.kind == "cache") is True


def test_cache_untouched_outside_requests():
    from django.core.cache import cache

    cache.set("k", 1)
    assert cache.get("k") == 1
    assert cache.get("missing", "dflt") == "dflt"


def test_template_spans(client, latest):
    client.get("/templated/")
    names = [s.data["template"] for s in latest().spans if s.kind == "template"]
    assert names[0] == "testapp/page.html"
    assert names.count("testapp/row.html") == 3


def test_no_trace_outside_requests_is_free(shop):
    from tests.testapp.models import Order

    assert Order.objects.count() == 6  # the wrapper is a pass-through without a trace


@pytest.mark.django_db(transaction=True)
async def test_async_view(async_client, shop, latest):
    response = await async_client.get("/async/", headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    trace = await sync_to_async(latest)()
    assert trace.view == "tests.testapp.views.async_view"
    assert trace.request["headers"]["Authorization"] != "Bearer x"
    view = next(s for s in trace.spans if s.kind == "view")
    assert view.data["async"] is True
    assert trace.query_count == 1
    assert any(s.kind == "cache" and s.data["op"] == "get" for s in trace.spans)


@pytest.mark.django_db(transaction=True)
async def test_async_streaming_and_exception(async_client, latest):
    response = await async_client.get("/stream/")
    assert response.status_code == 200
    trace = await sync_to_async(latest)()
    assert trace.response["body"] is None
    with pytest.raises(ValueError):
        await async_client.get("/boom/")
    trace = await sync_to_async(latest)()
    assert trace.status == 500 and trace.error["type"] == "ValueError"


@pytest.mark.django_db(transaction=True)
async def test_concurrent_async_requests_do_not_mix(async_client, shop, latest, rt):
    import asyncio

    await asyncio.gather(*(async_client.get(f"/plain/{i}/") for i in range(8)))
    await sync_to_async(rt.store.flush)(5.0)
    page = await sync_to_async(rt.store.list)(None, limit=20)
    assert sorted(t["path"] for t in page.items) == sorted(f"/plain/{i}/" for i in range(8))


def test_trace_header_only_in_debug(client, settings):
    assert "X-Redline-Trace" not in client.get("/plain/").headers
    settings.DEBUG = True
    assert "X-Redline-Trace" in client.get("/plain/").headers
