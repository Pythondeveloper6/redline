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
