"""``GET api/metrics``: Prometheus text, behind the same access rules as the rest of the API."""

from __future__ import annotations

from .conftest import client

TOKEN = "tok-" + "x" * 30
REMOTE = ("203.0.113.9", 4444)


def test_metrics_text(make_env):
    env = make_env(guards=None)
    with client(env) as c:
        r = c.get("/_redline/api/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "# TYPE redline_traces_saved_total counter" in r.text


def test_metrics_follow_the_access_policy(make_env):
    env = make_env(guards=None, settings={"access": "token", "token": TOKEN})
    with client(env, remote=REMOTE, base_url="https://redline.example") as c:
        assert c.get("/_redline/api/metrics").status_code == 401
        ok = c.get("/_redline/api/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200 and "redline_traces_saved_total" in ok.text
    env = make_env(guards=None)
    with client(env, remote=REMOTE) as c:
        assert c.get("/_redline/api/metrics").status_code == 403
