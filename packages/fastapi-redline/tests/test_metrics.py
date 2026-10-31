"""``GET api/metrics``: Prometheus text, behind the same access rules as the rest of the API."""

from __future__ import annotations

from fastapi.testclient import TestClient

TOKEN = "tok-" + "x" * 30
REMOTE = ("203.0.113.9", 4444)
LOCAL = "http://localhost"


def test_metrics_text(make_env):
    env = make_env(dependencies=None)
    with TestClient(env.app, base_url=LOCAL) as c:
        c.get("/orders/1")
        env.store.flush(5)
        r = c.get("/_redline/api/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "# TYPE redline_traces_saved_total counter" in r.text
    assert 'redline_traces_dropped_total{reason="write_failed"} 0' in r.text


def test_metrics_follow_the_access_policy(make_env):
    env = make_env(dependencies=None, settings={"access": "token", "token": TOKEN})
    with TestClient(env.app, client=REMOTE, base_url="https://redline.example") as c:
        assert c.get("/_redline/api/metrics").status_code == 401
        ok = c.get("/_redline/api/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200 and "redline_writer_queue_depth" in ok.text
    env = make_env(dependencies=None)
    with TestClient(env.app, client=REMOTE) as c:
        assert c.get("/_redline/api/metrics").status_code == 403
