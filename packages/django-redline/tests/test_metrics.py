"""``GET api/metrics``: Prometheus text, behind the same access rules as the rest of the API."""

from __future__ import annotations

import pytest
from django.test import Client

from django_redline.runtime import get_runtime

TOKEN = "tok-" + "x" * 30
REMOTE = {"REMOTE_ADDR": "203.0.113.9"}
BASE = {"STORAGE_PATH": ":memory:", "SAMPLE_RATE": 1.0}


@pytest.fixture
def redline(settings):
    def apply(**extra):
        settings.REDLINE = {**BASE, **extra}
        return get_runtime()

    return apply


def test_metrics_for_an_authorised_user(redline, staff_client, db):
    redline()
    r = staff_client.get("/redline/api/metrics")
    assert r.status_code == 200
    assert r["Content-Type"].startswith("text/plain; version=0.0.4")
    assert "# TYPE redline_traces_saved_total counter" in r.content.decode()


def test_metrics_follow_the_access_policy(redline, db):
    redline(ACCESS="token", TOKEN=TOKEN)
    c = Client()
    assert c.get("/redline/api/metrics", **REMOTE).status_code == 401
    ok = c.get("/redline/api/metrics", HTTP_AUTHORIZATION=f"Bearer {TOKEN}", **REMOTE)
    assert ok.status_code == 200 and b"redline_writer_queue_depth" in ok.content


def test_metrics_refuse_anonymous_users_by_default(redline, db):
    redline()
    assert Client().get("/redline/api/metrics").status_code in (302, 403)
