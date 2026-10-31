from __future__ import annotations

import logging

import pytest

from django_redline import install_if_dev, urls_if_dev
from django_redline.dev import APP, MIDDLEWARE


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("APP_ENV", "ENV", "ENVIRONMENT", "REDLINE_ENV", "DJANGO_ENV", "FLASK_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REDLINE_ALLOW_PRODUCTION", raising=False)


def test_adds_app_and_middleware_with_debug_on():
    ns = {"DEBUG": True, "INSTALLED_APPS": ["a"], "MIDDLEWARE": ["m"]}
    assert install_if_dev(ns) is True
    assert ns["INSTALLED_APPS"] == ["a", APP]
    assert ns["MIDDLEWARE"] == [MIDDLEWARE, "m"]
    assert install_if_dev(ns) is True  # idempotent
    assert ns["INSTALLED_APPS"].count(APP) == 1 and ns["MIDDLEWARE"].count(MIDDLEWARE) == 1


def test_debug_false_refuses(caplog):
    ns = {"DEBUG": False, "INSTALLED_APPS": ["a"], "MIDDLEWARE": ["m"]}
    with caplog.at_level(logging.WARNING, logger="redline"):
        assert install_if_dev(ns) is False
    assert ns["INSTALLED_APPS"] == ["a"] and ns["MIDDLEWARE"] == ["m"]
    assert "DEBUG is False" in caplog.text


def test_environment_refuses_even_with_debug_on(monkeypatch):
    monkeypatch.setenv("DJANGO_ENV", "production")
    ns = {"DEBUG": True, "INSTALLED_APPS": [], "MIDDLEWARE": []}
    assert install_if_dev(ns) is False and ns["INSTALLED_APPS"] == []


def test_override_attaches(monkeypatch):
    monkeypatch.setenv("REDLINE_ALLOW_PRODUCTION", "1")
    ns = {"DEBUG": False, "INSTALLED_APPS": [], "MIDDLEWARE": []}
    assert install_if_dev(ns) is True and APP in ns["INSTALLED_APPS"]


def test_urls_if_dev_follows_debug(settings):
    settings.DEBUG = True
    patterns = urls_if_dev("tools/")
    assert len(patterns) == 1 and str(patterns[0].pattern) == "tools/"
    settings.DEBUG = False
    assert urls_if_dev() == []
