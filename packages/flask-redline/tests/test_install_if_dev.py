from __future__ import annotations

import logging

from flask import Flask

from flask_redline import install_if_dev


def _settings(tmp_path):
    return {"storage_path": str(tmp_path / "r.db")}


def _clean(monkeypatch):
    for name in ("APP_ENV", "ENV", "ENVIRONMENT", "REDLINE_ENV", "FLASK_ENV", "DJANGO_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REDLINE_ALLOW_PRODUCTION", raising=False)


def test_installs_in_development(tmp_path, monkeypatch):
    _clean(monkeypatch)
    app = Flask(__name__)
    redline = install_if_dev(app, settings=_settings(tmp_path))
    try:
        assert redline is not None and "redline" in app.extensions
    finally:
        redline.close()


def test_refuses_for_environment_variable(tmp_path, monkeypatch, caplog):
    _clean(monkeypatch)
    monkeypatch.setenv("FLASK_ENV", "production")
    app = Flask(__name__)
    with caplog.at_level(logging.WARNING, logger="redline"):
        assert install_if_dev(app, settings=_settings(tmp_path)) is None
    assert "redline" not in app.extensions and "not attached" in caplog.text


def test_refuses_for_app_config(tmp_path, monkeypatch):
    _clean(monkeypatch)
    app = Flask(__name__)
    app.config["ENV"] = "production"
    assert install_if_dev(app, settings=_settings(tmp_path)) is None
    assert "redline" not in app.extensions


def test_override(tmp_path, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REDLINE_ALLOW_PRODUCTION", "1")
    app = Flask(__name__)
    redline = install_if_dev(app, settings=_settings(tmp_path))
    try:
        assert redline is not None
    finally:
        redline.close()
