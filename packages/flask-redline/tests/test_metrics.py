"""``GET api/metrics``: Prometheus text, behind the same access rules as the rest of the API."""

from __future__ import annotations

TOKEN = "tok-" + "x" * 30
REMOTE = {"REMOTE_ADDR": "203.0.113.9"}
HTTPS_REMOTE = {**REMOTE, "wsgi.url_scheme": "https"}


def test_metrics_text_and_counters(make_env):
    env = make_env()
    c = env.client()
    c.get("/orders/1")
    env.store.flush(5)
    r = c.get("/_redline/api/metrics")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/plain; version=0.0.4")
    text = r.get_data(as_text=True)
    assert "# TYPE redline_traces_saved_total counter" in text
    saved = [ln for ln in text.splitlines() if ln.startswith("redline_traces_saved_total ")]
    assert int(saved[0].split()[1]) >= 1
    assert 'redline_traces_dropped_total{reason="queue_full"} 0' in text
    assert "redline_writer_queue_depth" in text


def test_metrics_follow_the_access_policy(make_env):
    env = make_env()  # access = local
    assert env.client().get("/_redline/api/metrics", environ_overrides=REMOTE).status_code == 403

    env = make_env(settings={"access": "token", "token": TOKEN})
    c = env.client()
    kw = {"environ_overrides": HTTPS_REMOTE, "base_url": "https://redline.example"}
    assert c.get("/_redline/api/metrics", **kw).status_code == 401
    ok = c.get("/_redline/api/metrics", headers={"Authorization": f"Bearer {TOKEN}"}, **kw)
    assert ok.status_code == 200 and "redline_traces_saved_total" in ok.get_data(as_text=True)


def test_metrics_is_get_only(make_env):
    env = make_env()
    assert env.client().post("/_redline/api/metrics").status_code in (403, 405)
