from __future__ import annotations

import json
import re

import pytest
from django.test import Client, override_settings

pytestmark = pytest.mark.django_db


def test_index_requires_login(client):
    response = client.get("/redline/")
    assert response.status_code == 302
    assert response["Location"].startswith("/admin/login/?next=/redline/")


def test_index_forbidden_for_non_staff(client, django_user_model):
    user = django_user_model.objects.create_user("u", password="pw")
    client.force_login(user)
    assert client.get("/redline/").status_code == 403


def test_index_for_staff_embeds_csrf_meta(staff_client):
    response = staff_client.get("/redline/")
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    body = response.content.decode()
    assert '<meta name="redline-csrf"' in body and 'data-header="X-CSRFToken"' in body
    assert "csrftoken" in response.cookies


def test_assets_served_with_cache_headers(staff_client):
    from redline_core import ui

    page = ui.index_html()
    found = re.search(r'\./assets/([^"]+\.js)', page)
    if not found:
        pytest.skip("UI bundle not built")
    response = staff_client.get(f"/redline/assets/{found.group(1)}")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/javascript")
    assert "immutable" in response["Cache-Control"]
    response.close()
    assert staff_client.get("/redline/assets/../../conf.py").status_code == 404
    assert staff_client.get("/redline/assets/nope.js").status_code == 404


def test_api_forbidden_json_for_anonymous(client):
    response = client.get("/redline/api/meta")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_api_meta_and_traces(staff_client, client, latest):
    client.get("/plain/")
    latest()
    meta = staff_client.get("/redline/api/meta").json()
    assert meta["framework"] == "django"
    traces = staff_client.get("/redline/api/traces?limit=10").json()
    assert traces["total"] == 1
    trace_id = traces["items"][0]["id"]
    detail = staff_client.get(f"/redline/api/traces/{trace_id}").json()
    assert detail["path"] == "/plain/"
    assert staff_client.get("/redline/api/traces?status=bogus").status_code == 400
    assert staff_client.get("/redline/api/nope").status_code == 404
    assert staff_client.get("/redline/api/endpoints?window=1h").status_code == 200
    assert staff_client.get("/redline/api/issues").status_code == 200


def _csrf_client(staff) -> tuple[Client, str]:
    c = Client(enforce_csrf_checks=True)
    c.force_login(staff)
    page = c.get("/redline/").content.decode()
    token = re.search(r'name="redline-csrf" content="([^"]+)"', page).group(1)
    return c, token


def test_delete_requires_csrf_token(staff, client, latest):
    client.get("/plain/")
    latest()
    c, token = _csrf_client(staff)
    response = c.delete("/redline/api/traces")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    response = c.delete("/redline/api/traces", headers={"X-CSRFToken": token})
    assert response.status_code == 200
    assert response.json() == {"deleted": 1}


def test_explain_sqlite_through_api(staff, client, shop, latest):
    client.get("/n1/reverse-fk/")
    trace = latest()
    span = next(s for s in trace.spans if s.kind == "sql" and "orderitem" in s.data["sql"])
    c, token = _csrf_client(staff)
    body = json.dumps({"trace_id": trace.id, "span_id": span.id})
    assert c.post("/redline/api/explain", body, content_type="application/json").status_code == 403
    response = c.post(
        "/redline/api/explain",
        body,
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 200, response.content
    result = response.json()
    assert result["vendor"] == "sqlite"
    assert result["tree"]
    analyze = json.dumps({"trace_id": trace.id, "span_id": span.id, "analyze": True})
    response = c.post(
        "/redline/api/explain",
        analyze,
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )
    assert response.json()["error"]["code"] == "analyze_disabled"


def test_api_method_not_allowed(staff_client):
    assert staff_client.put("/redline/api/meta").status_code == 405


def allow_everyone(request):
    return True


@override_settings(
    REDLINE={"STORAGE_PATH": ":memory:", "PERMISSION": "tests.test_views.allow_everyone"}
)
def test_custom_permission(client):
    assert client.get("/redline/api/meta").status_code == 200
    assert client.get("/redline/").status_code == 200
