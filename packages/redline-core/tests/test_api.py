from __future__ import annotations

import json
import sqlite3
import time

import pytest

from redline_core import __version__
from redline_core.api import RedlineAPI
from redline_core.conf import Settings
from redline_core.detect import detect

from .conftest import make_callsite, make_trace, sql_span


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY, order_id INT);")
    yield conn
    conn.close()


@pytest.fixture
def api(store, db):
    def factory(alias, vendor):
        if alias != "default" or vendor != "sqlite":
            return None
        # like Django, the adapter converts the format paramstyle for sqlite3
        return lambda sql, params: db.execute(sql.replace("%s", "?"), params or ()).fetchall()

    return RedlineAPI(store, Settings(), framework="django", explain_executor=factory)


@pytest.fixture
def seeded(store):
    cs = make_callsite()
    spans = [
        sql_span("SELECT * FROM items WHERE order_id = %s", [i], callsite=cs) for i in range(5)
    ]
    spans.append(sql_span("UPDATE items SET order_id = %s", [1]))
    t = make_trace(spans=spans, profile={"profiles": [], "shared": {"frames": []}})
    t.issues = detect(t)
    store.save(t)
    other = make_trace(path="/x", route="/x", status=500, started_at=time.time() - 1)
    store.save(other)
    return t, other


def test_meta(api, seeded):
    status, data = api.dispatch("GET", "/api/meta/")
    assert status == 200
    assert data["version"] == __version__ and data["framework"] == "django"
    assert data["storage"]["rows"] == 2 and data["storage"]["path"].endswith("redline.db")
    s = data["settings"]
    assert set(s) >= {"thresholds", "sampling", "profiling_enabled", "explain_analyze_enabled"}
    json.dumps(data)


def test_list_and_filters(api, seeded):
    t, other = seeded
    status, data = api.dispatch("GET", "api/traces", {"limit": "1"})
    assert status == 200 and data["total"] == 2 and len(data["items"]) == 1
    assert data["items"][0]["id"] == t.id and data["next_cursor"]
    status, data2 = api.dispatch(
        "GET", "api/traces", {"limit": ["1"], "cursor": data["next_cursor"]}
    )
    assert [i["id"] for i in data2["items"]] == [other.id] and data2["next_cursor"] is None
    assert api.dispatch("GET", "api/traces", {"status": "5xx"})[1]["total"] == 1
    assert api.dispatch("GET", "api/traces", {"has_issues": "1"})[1]["items"][0]["issue_kinds"] == [
        "n_plus_one"
    ]
    status, err = api.dispatch("GET", "api/traces", {"status": "9xx"})
    assert status == 400 and err["error"]["code"] == "invalid_filter"
    assert api.dispatch("GET", "api/traces", {"limit": "abc"})[0] == 400
    assert api.dispatch("GET", "api/traces", {"cursor": "%%%"})[0] == 400


def test_trace_detail_and_profile(api, seeded):
    t, other = seeded
    status, data = api.dispatch("GET", f"api/traces/{t.id}")
    assert status == 200 and data["has_profile"] is True and "profile" not in data
    assert len(data["spans"]) == 6 and data["issues"][0]["kind"] == "n_plus_one"
    status, prof = api.dispatch("GET", f"api/traces/{t.id}/profile")
    assert status == 200 and "profiles" in prof
    assert (
        api.dispatch("GET", f"api/traces/{other.id}/profile")[1]["error"]["code"]
        == "profile_not_found"
    )
    status, err = api.dispatch("GET", "api/traces/doesnotexist")
    assert status == 404 and err["error"]["code"] == "trace_not_found"


def test_delete_and_clear(api, seeded):
    t, _ = seeded
    assert api.dispatch("DELETE", f"api/traces/{t.id}") == (200, {"deleted": 1})
    assert api.dispatch("DELETE", f"api/traces/{t.id}") == (200, {"deleted": 0})
    assert api.dispatch("DELETE", "api/traces") == (200, {"deleted": 1})


def test_endpoints_and_issues(api, seeded):
    status, data = api.dispatch("GET", "api/endpoints", {"window": "1h"})
    assert status == 200 and {e["route"] for e in data["items"]} == {"/orders/<int:pk>/", "/x"}
    assert api.dispatch("GET", "api/endpoints", {"window": "1y"})[0] == 400
    status, data = api.dispatch("GET", "api/issues", {"window": "7d", "kind": "n_plus_one"})
    assert status == 200 and data["items"][0]["occurrences"] == 1
    assert set(data["items"][0]) == {
        "id",
        "kind",
        "severity",
        "title",
        "detail",
        "occurrences",
        "first_seen",
        "last_seen",
        "routes",
        "sample_trace_id",
        "sample_span_ids",
        "suggestion",
        "callsite",
        "suppressed",
        "suppressed_routes",
        "observed_total_ms",
        "fingerprint",
    }
    assert api.dispatch("GET", "api/issues", {"kind": "bogus"})[0] == 400
    assert api.dispatch("GET", "api/issues")[0] == 200


def test_explain(api, seeded):
    t, _ = seeded
    span = t.spans[0]
    status, data = api.dispatch(
        "POST", "api/explain", body=json.dumps({"trace_id": t.id, "span_id": span.id}).encode()
    )
    assert status == 200, data
    assert data["vendor"] == "sqlite" and data["findings"][0]["title"] == "Full table scan on items"
    assert data["suggested_indexes"] == ["CREATE INDEX items_order_id_idx ON items (order_id);"]
    json.dumps(data)


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (b"not json", 400, "invalid_json"),
        (b"[]", 400, "invalid_body"),
        ({"trace_id": 1}, 400, "invalid_body"),
        ({"trace_id": "nope", "span_id": "x"}, 404, "trace_not_found"),
        ({"trace_id": "T", "span_id": "nope"}, 404, "span_not_found"),
        ({"trace_id": "T", "span_id": "UPDATE"}, 400, "not_explainable"),
        ({"trace_id": "T", "span_id": "S", "analyze": True}, 403, "analyze_disabled"),
    ],
)
def test_explain_errors(api, seeded, body, status, code):
    t, _ = seeded
    if isinstance(body, dict):
        body = {
            k: (
                t.id
                if v == "T"
                else t.spans[0].id
                if v == "S"
                else t.spans[-1].id
                if v == "UPDATE"
                else v
            )
            for k, v in body.items()
        }
    got_status, data = api.dispatch("POST", "api/explain", body=body)
    assert (got_status, data["error"]["code"]) == (status, code)


def test_explain_unavailable(store, seeded):
    t, _ = seeded
    api = RedlineAPI(store)
    status, data = api.explain({"trace_id": t.id, "span_id": t.spans[0].id})
    assert status == 501 and data["error"]["code"] == "explain_unavailable"


def test_routing_errors_and_internal_errors(api, store):
    assert api.dispatch("GET", "api/nope")[0] == 404
    assert api.dispatch("POST", "api/meta")[0] == 405
    assert api.dispatch("PUT", "api/traces")[0] == 405
    assert api.dispatch("GET", "api/explain")[0] == 405

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    store.count = boom
    status, data = api.dispatch("GET", "api/meta")
    assert status == 500 and data["error"]["code"] == "internal_error"
