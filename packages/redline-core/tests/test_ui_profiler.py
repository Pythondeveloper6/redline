from __future__ import annotations

import builtins
import time

import pytest

from redline_core import profiler, ui


def test_static_dir_and_index():
    assert ui.static_dir().name == "static"
    html = ui.index_html()
    assert "<html" in html.lower()


def test_asset_path_refuses_traversal():
    assert ui.asset_path("../__init__.py") is None
    assert ui.asset_path("/../../etc/passwd") is None
    assert ui.asset_path("") is None
    assert ui.asset_path("nope.js") is None
    assert ui.asset_path("assets/../../ui/__init__.py") is None
    assert ui.asset_path("assets/..\\..\\__init__.py") is None
    assert ui.asset_path("a\x00b") is None
    if ui.is_built():
        assert ui.asset_path("index.html") is not None


def test_content_type():
    assert ui.content_type("a.js") == "text/javascript; charset=utf-8"
    assert ui.content_type("a.css") == "text/css; charset=utf-8"
    assert ui.content_type("a.woff2").startswith(("font/", "application/"))


def test_profiler_missing_gives_clear_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("pyinstrument"):
            raise ImportError("no pyinstrument")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert profiler.is_available() is False
    with pytest.raises(profiler.ProfilerUnavailable, match=r"redline-core\[profile\]"):
        profiler.require()
    with pytest.raises(ImportError, match="pip install"):
        profiler.RequestProfiler()


def test_profiler_missing_is_tagged_not_raised(monkeypatch):
    from redline_core.conf import Settings
    from redline_core.recorder import Recorder

    def broken(*a, **k):
        raise profiler.ProfilerUnavailable("install it")

    monkeypatch.setattr(profiler, "RequestProfiler", broken)
    rec = Recorder(Settings(profile="all"))
    b = rec.start_trace(method="GET", path="/")
    t = rec.finish_trace(b, status=200)
    assert t.profile is None and "install it" in t.tags["profile_error"]


def test_request_profiler_roundtrip():
    pytest.importorskip("pyinstrument")
    p = profiler.RequestProfiler()
    assert p.stop() is None  # never started
    p.start()
    sum(i * i for i in range(200_000))
    doc = p.stop()
    assert doc["$schema"].startswith("https://www.speedscope.app")
    p2 = profiler.RequestProfiler()
    p2.start()
    p2.abort()


def _doc(files):
    frames = [{"name": f"f{i}", "file": f, "line": 1} for i, f in enumerate(files)]
    n = len(files)
    events = [{"type": "O", "at": 0.0, "frame": i} for i in range(n)]
    events += [{"type": "C", "at": 1.0, "frame": i} for i in reversed(range(n))]
    return {"shared": {"frames": frames}, "profiles": [{"type": "evented", "events": events}]}


def test_collapse_framework_frames_keeps_total_and_app_frames():
    doc = _doc(
        [
            "/x/django/utils/deprecation.py",
            "/x/django/core/handlers/exception.py",
            "/x/django_redline/middleware.py",
            "/x/shop/views.py",
            "/x/django/db/models/query.py",
        ]
    )
    assert profiler.collapse_framework_frames(doc) == 3
    events = doc["profiles"][0]["events"]
    names = [doc["shared"]["frames"][e["frame"]]["name"] for e in events if e["type"] == "O"]
    assert names == ["[framework] 3 frames", "f3", "f4"]
    assert events[0]["at"] == 0.0 and events[-1]["at"] == 1.0 and events[-1]["type"] == "C"
    assert sum(e["type"] == "O" for e in events) == sum(e["type"] == "C" for e in events)


def test_collapse_framework_frames_noop_without_framework_root():
    doc = _doc(["/x/shop/views.py", "/x/django/core/handlers/base.py"])
    before = [dict(e) for e in doc["profiles"][0]["events"]]
    assert profiler.collapse_framework_frames(doc) == 0
    assert doc["profiles"][0]["events"] == before


def test_profile_title_is_the_request():
    pytest.importorskip("pyinstrument")
    from redline_core.conf import Settings
    from redline_core.recorder import Recorder

    rec = Recorder(Settings(profile="all"))
    b = rec.start_trace(method="GET", path="/orders/")
    end = time.perf_counter() + 0.02  # long enough for samples on a busy machine
    while time.perf_counter() < end:
        sum(range(100))
    t = rec.finish_trace(b, status=200, route="/orders/", view="shop.views.order_list")
    want = "GET /orders/ — shop.views.order_list"
    assert t.profile["name"] == want and t.profile["profiles"][0]["name"] == want
