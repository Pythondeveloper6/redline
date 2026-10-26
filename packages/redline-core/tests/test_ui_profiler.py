from __future__ import annotations

import builtins

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
