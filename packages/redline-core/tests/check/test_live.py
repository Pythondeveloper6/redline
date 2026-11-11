from __future__ import annotations

import threading

import pytest

httpx = pytest.importorskip("httpx")

from redline_core.check.live import run_live_compare  # noqa: E402
from redline_core.check.suite import SuiteError, _parse_suite  # noqa: E402


def make_suite(**extra):
    data = {
        "name": "shop",
        "base_url": "http://unused.test",
        "requests": [{"name": "list", "path": "/orders"}, {"name": "item", "path": "/orders/1"}],
    }
    data.update(extra)
    return _parse_suite(data, default_name="shop", source=None)


def server(log, tag, body=None, fail=False):
    def handler(request):
        log.append((tag, request.url.path))
        if fail:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json=body if body is not None else {"ok": True})

    return httpx.MockTransport(handler)


def test_requests_alternate_and_flip_each_round():
    log = []
    out = run_live_compare(
        make_suite(),
        old_url="http://old.test",
        new_url="http://new.test",
        rounds=2,
        transport_old=server(log, "old"),
        transport_new=server(log, "new"),
    )
    first_request = [t for t, p in log if p == "/orders"]
    assert first_request == ["old", "new", "new", "old"]  # ABBA
    assert {r["name"] for r in out["requests"]} == {"list", "item"}
    assert all(len(r["old_ms"]) == 2 and len(r["new_ms"]) == 2 for r in out["requests"])


def test_changed_response_is_flagged():
    out = run_live_compare(
        make_suite(),
        old_url="http://old.test",
        new_url="http://new.test",
        rounds=1,
        transport_old=server([], "old", {"n": 1}),
        transport_new=server([], "new", {"n": 2}),
    )
    assert all(r["responses_equal"] is False for r in out["requests"])


def test_unreachable_server_is_reported_not_hung():
    out = run_live_compare(
        make_suite(),
        old_url="http://old.test",
        new_url="http://new.test",
        rounds=2,
        transport_old=server([], "old"),
        transport_new=server([], "new", fail=True),
    )
    row = out["requests"][0]
    assert row["errors"]["new"] == {"ConnectError": 2}
    assert row["new_ms"] == [] and row["median_delta_ms"] is None
    assert row["responses_equal"] is None


def test_stop_event_returns_partial_result():
    stop = threading.Event()
    stop.set()
    out = run_live_compare(
        make_suite(),
        old_url="http://old.test",
        new_url="http://new.test",
        rounds=3,
        transport_old=server([], "old"),
        transport_new=server([], "new"),
        stop=stop,
    )
    assert out["status"] == "stopped" and out["rounds_done"] == 0


def test_rounds_validated():
    with pytest.raises(ValueError):
        run_live_compare(make_suite(), old_url="http://a.test", new_url="http://b.test", rounds=0)


def test_urls_validated():
    with pytest.raises(SuiteError):
        run_live_compare(make_suite(), old_url="ftp://a.test", new_url="http://b.test")


def test_cli_compare_live(tmp_path, capsys, monkeypatch):
    import json

    from redline_core import cli
    from redline_core.check import live

    suite = tmp_path / "s.json"
    suite.write_text(json.dumps({"name": "shop", "requests": [{"name": "list", "path": "/o"}]}))
    seen = {}

    def fake(suite_obj, **kw):
        seen.update(kw)
        return {
            "status": "done",
            "rounds_done": kw["rounds"],
            "requests": [
                {
                    "name": "list",
                    "method": "GET",
                    "path": "/o",
                    "old_ms": [1.0],
                    "new_ms": [2.0],
                    "median_old": 1.0,
                    "median_new": 2.0,
                    "median_delta_ms": 1.0,
                    "responses_equal": True,
                    "errors": {"old": {}, "new": {}},
                }
            ],
        }

    monkeypatch.setattr(live, "run_live_compare", fake)
    code = cli.main(
        [
            "check",
            "compare-live",
            str(suite),
            "--old",
            "http://a.test",
            "--new",
            "http://b.test",
            "--rounds",
            "3",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0 and seen["rounds"] == 3 and "list" in out and "+1.0" in out


def test_cli_compare_live_rejects_bad_url(tmp_path):
    import json

    from redline_core import cli

    suite = tmp_path / "s.json"
    suite.write_text(json.dumps({"name": "shop", "requests": [{"name": "list", "path": "/o"}]}))
    bad = ["check", "compare-live", str(suite), "--old", "nope", "--new", "http://b.test"]
    assert cli.main(bad) == 2
