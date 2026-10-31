from __future__ import annotations

import logging

from litestar import Litestar

from litestar_redline import RedlinePlugin, install_if_dev


def _clean(monkeypatch):
    for name in ("APP_ENV", "ENV", "ENVIRONMENT", "REDLINE_ENV", "FLASK_ENV", "DJANGO_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REDLINE_ALLOW_PRODUCTION", raising=False)


def test_returns_a_plugin_in_development(tmp_path, monkeypatch):
    _clean(monkeypatch)
    plugin = install_if_dev(settings={"storage_path": str(tmp_path / "r.db")})
    try:
        assert isinstance(plugin, RedlinePlugin)
        app = Litestar([], plugins=[plugin])
        assert any("_redline" in str(r.path) for r in app.routes)
    finally:
        plugin.close()


def test_returns_none_in_production(tmp_path, monkeypatch, caplog):
    _clean(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    with caplog.at_level(logging.WARNING, logger="redline"):
        plugin = install_if_dev(settings={"storage_path": str(tmp_path / "r.db")})
    assert plugin is None and "not attached" in caplog.text
    assert not (tmp_path / "r.db").exists()
    app = Litestar([], plugins=[plugin] if plugin else [])
    assert not any("_redline" in str(r.path) for r in app.routes)


def test_override(tmp_path, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("REDLINE_ALLOW_PRODUCTION", "1")
    plugin = install_if_dev(settings={"storage_path": str(tmp_path / "r.db")})
    try:
        assert plugin is not None
    finally:
        plugin.close()
