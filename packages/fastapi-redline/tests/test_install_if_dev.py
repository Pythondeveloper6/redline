from __future__ import annotations

import logging

from fastapi import FastAPI

from fastapi_redline import install_if_dev


def _settings(tmp_path):
    return {"storage_path": str(tmp_path / "r.db")}


def test_installs_in_development(tmp_path, monkeypatch):
    for name in ("APP_ENV", "ENV", "ENVIRONMENT", "REDLINE_ENV"):
        monkeypatch.delenv(name, raising=False)
    app = FastAPI()
    redline = install_if_dev(app, settings=_settings(tmp_path))
    try:
        assert redline is not None and app.state.redline is redline
    finally:
        redline.close()


def test_refuses_in_production(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("REDLINE_ALLOW_PRODUCTION", raising=False)
    app = FastAPI()
    with caplog.at_level(logging.WARNING, logger="redline"):
        assert install_if_dev(app, settings=_settings(tmp_path)) is None
    assert getattr(app.state, "redline", None) is None
    assert not (tmp_path / "r.db").exists()
    assert all("redline" not in getattr(r, "path", "") for r in app.routes)
    assert "not attached" in caplog.text


def test_override_installs_in_production(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("REDLINE_ALLOW_PRODUCTION", "1")
    app = FastAPI()
    redline = install_if_dev(app, settings=_settings(tmp_path))
    try:
        assert redline is not None
    finally:
        redline.close()
