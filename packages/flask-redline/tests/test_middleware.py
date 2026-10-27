from __future__ import annotations

import json
import threading

import pytest
from werkzeug.test import EnvironBuilder
from werkzeug.wsgi import FileWrapper

from flask_redline import Redline


def test_sync_view_trace(make_env):
    env = make_env()
    r = env.client().get("/orders/3?x=1", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    t = env.trace()
    assert t.framework == "flask"
    assert t.method == "GET" and t.path == "/orders/3"
    assert t.route == "/orders/<int:order_id>"
    assert t.view == "tests.shop_app.get_order"
    assert t.status == 200
    assert t.query_count == 1
    sql = next(s for s in t.spans if s.kind == "sql")
    assert sql.data["vendor"] == "sqlite" and sql.data["db_alias"] == "default"
    assert sql.callsite and sql.callsite.file == "tests/shop_app.py"
    assert "select(Order)" in sql.callsite.code
    assert t.request["headers"]["Authorization"] != "Bearer s3cret"
    assert "s3cret" not in json.dumps(t.request)
    assert t.request["query"] == {"x": "1"}
    assert json.loads(t.response["body"]) == {"id": 3, "note": "n2"}
    assert t.response["size"] == len(r.data)


def test_view_span_wraps_only_the_view(make_env):
    env = make_env()
    env.client().get("/orders/1")
    t = env.trace()
    view = next(s for s in t.spans if s.kind == "view")
    assert view.name == "tests.shop_app.get_order"
    sql = next(s for s in t.spans if s.kind == "sql")
    assert sql.parent_id == view.id
    assert view.duration_ms <= t.duration_ms


def test_route_template_for_converters_and_script_root(make_env):
    env = make_env()
    env.client().get("/slow/4")
    assert env.trace().route == "/slow/<int:n>"
    # a concrete path never leaks into the route; unmatched URLs have none
    env.client().get("/no/such/page")
    assert env.trace("/no/such/page").route is None


def test_concurrent_requests_are_isolated(make_env):
    env = make_env()
    results: list[int] = []

    def hit(n: int) -> None:
        results.append(env.client().get(f"/slow/{n}").status_code)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(1, 7)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results == [200] * 6
    traces = [env.store.get(t["id"]) for t in env.traces()]
    assert len(traces) == 6
    for t in traces:
        n = int(t.path.rsplit("/", 1)[1])
        params = [s.data["params"] for s in t.spans if s.kind == "sql"]
        assert params == [[n], [n]], (t.path, params)
        assert t.duration_ms >= 45


def test_body_replay_and_capture(make_env):
    env = make_env()
    payload = {"user": "ann", "password": "hunter2"}
    r = env.client().post("/echo", json=payload)
    assert json.loads(r.json["body"]) == payload  # the view saw it intact
    t = env.trace()
    body = json.loads(t.request["body"])
    assert body["user"] == "ann" and body["password"] != "hunter2"
    assert t.request["body_truncated"] is False


def test_large_request_body_is_capped_and_masked(make_env):
    env = make_env(settings={"max_body_bytes": 200})
    big = json.dumps({"password": "hunter2", "pad": "x" * 5000})
    r = env.client().post("/echo", data=big, content_type="application/json")
    assert r.json["len"] == len(big)  # the view got everything
    t = env.trace()
    assert t.request["body_truncated"] is True
    assert "hunter2" not in t.request["body"]
    assert len(t.request["body"]) <= 200


def test_streaming_response_is_capped_not_buffered(make_env):
    env = make_env(settings={"max_body_bytes": 500})
    r = env.client().get("/stream", buffered=False)
    # nothing has been consumed yet: the middleware did not read the generator ahead
    assert env.traces() == []
    data = r.get_data()
    assert data.count(b"\n") == 200
    t = env.trace()
    assert t.response["body_truncated"] is True
    assert len(t.response["body"]) <= 500
    assert t.response["size"] == len(data)
    # work done while streaming is still attributed to the request (and counted in its time)
    assert t.query_count == 1


def test_send_file_is_returned_untouched(make_env):
    env = make_env()
    environ = EnvironBuilder("/file").get_environ()
    seen = {}

    def start_response(status, headers, exc_info=None):
        seen["status"] = status
        seen["headers"] = dict(headers)

    iterable = env.app.wsgi_app(environ, start_response)
    try:
        assert isinstance(iterable, FileWrapper)  # not wrapped: servers can use sendfile
        body = b"".join(iterable)
    finally:
        iterable.close()
    assert seen["status"].startswith("200")
    t = env.trace("/file")
    assert t.status == 200
    assert t.response["size"] == len(body) == int(seen["headers"]["Content-Length"])
    assert t.response["body"] in (None, "")
    r = env.client().get("/file")
    assert r.data == body


def test_unhandled_exception_recorded(make_env):
    env = make_env()
    r = env.client().get("/boom")
    assert r.status_code == 500
    t = env.trace()
    assert t.status == 500
    assert t.error["type"] == "RuntimeError" and t.error["message"] == "kaboom"
    assert "kaboom" in t.error["traceback"]
    assert t.route == "/boom"
    assert t.tags["sampled_by"] in {"all", "error"}


def test_unhandled_exception_is_reraised_when_propagating(make_env):
    env = make_env()
    env.app.config["PROPAGATE_EXCEPTIONS"] = True
    with pytest.raises(RuntimeError, match="kaboom"):
        env.client().get("/boom")
    t = env.trace()
    assert t.status == 500 and t.error["message"] == "kaboom"


def test_4xx_is_not_an_error_but_5xx_is(make_env):
    env = make_env(settings={"sample_rate": 0.0})
    c = env.client()
    assert c.get("/missing").status_code == 404
    assert c.get("/forbidden").status_code == 403
    assert c.get("/orders/abc").status_code == 404  # converter mismatch
    assert env.traces() == []  # sample_rate 0 and no error: nothing kept
    assert c.get("/unavailable").status_code == 503  # abort(503): a 5xx is an error
    assert c.get("/boom").status_code == 500
    kept = {t["path"]: t for t in env.traces()}
    assert set(kept) == {"/unavailable", "/boom"}
    assert all(t["tags"]["sampled_by"] == "error" for t in kept.values())


def test_4xx_traces_have_no_error(make_env):
    env = make_env()
    env.client().get("/missing")
    t = env.trace()
    assert t.status == 404 and t.error is None
    assert t.route == "/missing"


def test_redline_prefix_is_ignored(make_env):
    env = make_env()
    c = env.client()
    assert c.get("/_redline/").status_code == 200
    assert c.get("/_redline/api/meta").status_code == 200
    assert c.get("/_redline/api/traces").status_code == 200
    c.get("/dup")
    assert [t["path"] for t in env.traces()] == ["/dup"]


def test_sampling_rate_zero_keeps_nothing_but_errors(make_env):
    env = make_env(settings={"sample_rate": 0.0})
    c = env.client()
    c.get("/dup")
    c.get("/boom")
    assert [t["path"] for t in env.traces()] == ["/boom"]


def test_disabled_records_nothing_and_installs_nothing(make_env):
    from flask_redline import RedlineMiddleware

    env = make_env(settings={"enabled": False})
    assert env.client().get("/dup").status_code == 200
    assert env.traces() == []
    assert not isinstance(env.app.wsgi_app, RedlineMiddleware)
    assert env.client().get("/_redline/api/meta").json["storage"]["rows"] == 0


def test_run_header_becomes_tags(make_env):
    env = make_env()
    env.client().get("/dup", headers={"X-Redline-Run": "run42;step=3"})
    t = env.trace()
    assert t.tags["run_id"] == "run42" and t.tags["step"] == "3"


def test_profile_opt_in_query_and_header(make_env):
    env = make_env()
    c = env.client()
    # /slow sleeps 50 ms, so the 1 ms sampler always catches something
    c.get("/slow/1")
    c.get("/slow/1?_redline=profile")
    c.get("/slow/2", headers={"X-Redline-Profile": "1"})
    items = {(t["path"], t["has_profile"]) for t in env.traces()}
    assert items == {("/slow/1", False), ("/slow/1", True), ("/slow/2", True)}
    prof_id = next(t["id"] for t in env.traces() if t["has_profile"])
    profile = env.store.get_profile(prof_id)
    assert profile["$schema"].startswith("https://www.speedscope.app")
    assert profile["profiles"]


def test_profile_off_mode_ignores_request(make_env):
    env = make_env(settings={"profile": "off"})
    env.client().get("/slow/1?_redline=profile")
    assert not env.traces()[0]["has_profile"]


def test_redaction_of_headers_query_and_response(make_env):
    env = make_env()
    env.client().get(
        "/orders/1?token=abc&page=2", headers={"Authorization": "Bearer zzz", "X-Api-Key": "k"}
    )
    t = env.trace()
    assert t.request["query"]["token"] != "abc" and t.request["query"]["page"] == "2"
    assert t.request["headers"]["Authorization"] != "Bearer zzz"
    assert t.request["headers"]["X-Api-Key"] != "k"


def test_async_view_keeps_the_trace(make_env):
    pytest.importorskip("asgiref")
    env = make_env()
    r = env.client().get("/async")
    assert r.status_code == 200 and r.json == {"id": 1}
    t = env.trace()
    assert t.route == "/async"
    assert t.query_count == 1
    sql = next(s for s in t.spans if s.kind == "sql")
    assert sql.callsite and sql.callsite.file == "tests/shop_app.py"
    view = next(s for s in t.spans if s.kind == "view")
    assert sql.parent_id == view.id


# ── templates ─────────────────────────────────────────────────────────────────────────


def test_template_span_nests_lazy_queries(make_env):
    env = make_env()
    assert env.client().get("/page").status_code == 200
    t = env.trace()
    tpl = next(s for s in t.spans if s.kind == "template")
    assert tpl.name == "orders.html" and tpl.data["template"] == "orders.html"
    lazy = [s for s in t.spans if s.kind == "sql" and s.parent_id == tpl.id]
    assert len(lazy) == 3  # order.items lazy-loaded once per order, inside the render
    assert t.query_count == 4


def test_failed_render_does_not_leave_a_dangling_span(make_env):
    env = make_env()
    assert env.client().get("/broken-page").status_code == 500
    t = env.trace()
    tpl = next(s for s in t.spans if s.kind == "template")
    assert tpl.name == "broken.html" and tpl.data.get("error")
    assert t.error is not None


# ── factory pattern, misc ─────────────────────────────────────────────────────────────


def test_factory_pattern(make_env):
    env = make_env(factory=True)
    assert env.redline.app is env.app
    assert env.app.extensions["redline"] is not None
    env.client().get("/orders/2")
    assert env.trace().route == "/orders/<int:order_id>"


def test_settings_from_app_config_are_merged_under_kwargs(tmp_path):
    from flask import Flask

    app = Flask(__name__)
    app.config["REDLINE"] = {
        "storage_path": str(tmp_path / "a.db"),
        "SLOW_QUERY_MS": 7,
        "n_plus_one_threshold": 9,
    }
    rl = Redline(app, settings={"n_plus_one_threshold": 3}, permission=lambda request: True)
    try:
        assert rl.settings.slow_query_ms == 7  # from app.config
        assert rl.settings.n_plus_one_threshold == 3  # explicit wins
    finally:
        rl.close()


def test_double_init_rejected(make_env):
    env = make_env()
    with pytest.raises(RuntimeError, match="already installed"):
        env.redline.init_app(env.app)


def test_bad_url_prefix():
    with pytest.raises(ValueError):
        Redline(url_prefix="/")


def test_custom_url_prefix_is_mounted_and_ignored(make_env):
    env = make_env(url_prefix="debug/redline/")
    c = env.client()
    assert c.get("/debug/redline/api/meta").status_code == 200
    c.get("/dup")
    assert [t["path"] for t in env.traces()] == ["/dup"]
    assert c.get("/_redline/api/meta").status_code == 404  # the default prefix is not mounted


def test_mounted_under_script_name(make_env):
    from werkzeug.middleware.dispatcher import DispatcherMiddleware

    env = make_env()
    from flask import Flask

    root = Flask("root")
    mounted = DispatcherMiddleware(root.wsgi_app, {"/shop": env.app})
    from werkzeug.test import Client

    r = Client(mounted).get("/shop/orders/5", buffered=True)
    assert r.status_code == 200
    t = env.trace()
    assert t.path == "/shop/orders/5"
    assert t.route == "/shop/orders/<int:order_id>"
    assert Client(mounted).get("/shop/_redline/api/meta", buffered=True).status_code == 200
    assert [x["path"] for x in env.traces()] == ["/shop/orders/5"]


def test_head_request(make_env):
    env = make_env()
    r = env.client().head("/orders/1")
    assert r.status_code == 200
    t = env.trace()
    assert t.method == "HEAD" and t.route == "/orders/<int:order_id>"
