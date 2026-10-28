from __future__ import annotations

import pytest

from redline_core.check import Suite
from redline_core.check.generate import from_traces

from ..conftest import make_trace


def save(store, *, request=None, **kw):
    t = make_trace(**kw)
    if request:
        t.request.update(request)
    store.save(t)
    return t


def test_from_traces(store):
    for i in range(3):
        save(store, path=f"/orders/{i}/", route="/orders/<int:pk>/", started_at=1_000 + i)
    save(
        store,
        path="/orders/",
        route="/orders/",
        request={
            "headers": {"Authorization": "[redacted]", "Cookie": "[redacted]"},
            "query": {"page": "2"},
        },
    )
    save(store, path="/cart/", route="/cart/", method="POST")
    save(store, path="/_redline/api/meta", route="/_redline/api/meta")
    save(store, path="/reset/[redacted]/", route="/reset/<token>/")
    save(store, path="/broken/", route="/broken/", status=500)
    save(store, path="/q/", route="/q/", request={"query": {"token": "[redacted]"}})
    data = from_traces(store, base_url="http://shop:9", window_s=10**10)
    reqs = {r["name"]: r for r in data["requests"]}
    assert set(reqs) == {"GET /orders/<int:pk>/", "GET /orders/"}
    assert reqs["GET /orders/<int:pk>/"]["path"] == "/orders/2/"  # most recent
    listing = reqs["GET /orders/"]
    assert listing["path"] == "/orders/?page=2"
    assert listing["headers"] == {
        "Authorization": "${env:REDLINE_CHECK_AUTHORIZATION}",
        "Cookie": "${env:REDLINE_CHECK_COOKIE}",
    }
    assert "[redacted]" not in str(data)
    suite = Suite.from_dict(data)
    assert suite.base_url == "http://shop:9"
    assert suite.env_names() == ["REDLINE_CHECK_AUTHORIZATION", "REDLINE_CHECK_COOKIE"]


def test_route_filter_and_limit(store):
    save(store, path="/a/1", route="/a/<id>")
    save(store, path="/b/1", route="/b/<id>")
    save(store, path="/b/2", route="/b/<id>/x")
    data = from_traces(store, routes=["/b/*"], window_s=10**10)
    assert sorted(r["name"] for r in data["requests"]) == ["GET /b/<id>", "GET /b/<id>/x"]
    assert len(from_traces(store, limit=1, window_s=10**10)["requests"]) == 1
    with pytest.raises(ValueError, match="for those routes"):
        from_traces(store, routes=["/zzz"], window_s=10**10)


def test_empty_store(store):
    with pytest.raises(ValueError, match="no recorded read-only traffic"):
        from_traces(store)


def test_route_with_recent_failures_uses_an_older_healthy_trace(store):
    # a refactor broke the route: its latest 12 traces are 500s, the healthy one is older
    save(store, path="/flaky/", route="/flaky/", started_at=1_000)
    for i in range(12):
        save(store, path="/flaky/", route="/flaky/", status=500, started_at=2_000 + i)
    data = from_traces(store, window_s=10**10)
    assert [r["name"] for r in data["requests"]] == ["GET /flaky/"]


def test_base_url_only_from_args_or_a_single_host(store):
    save(store, path="/a/", route="/a/")
    assert "base_url" not in from_traces(store, window_s=10**10)
    assert from_traces(store, base_url="http://x:1", window_s=10**10)["base_url"] == "http://x:1"
    save(store, path="/b/", route="/b/", request={"headers": {"Host": "shop.local:8000"}})
    # one trace has no host: still not certain
    assert "base_url" not in from_traces(store, window_s=10**10)


def test_base_url_from_one_host_in_the_traces(store):
    save(store, path="/a/", route="/a/", request={"headers": {"host": "shop.local:8000"}})
    save(store, path="/b/", route="/b/", request={"headers": {"Host": "shop.local:8000"}})
    assert from_traces(store, window_s=10**10)["base_url"] == "http://shop.local:8000"
    save(store, path="/c/", route="/c/", request={"headers": {"Host": "other.local"}})
    assert "base_url" not in from_traces(store, window_s=10**10)
