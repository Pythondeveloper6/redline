from __future__ import annotations

import logging

import pytest

from redline_core.config import parse_env
from redline_core.environment import production_signals, refuse_in_production


@pytest.mark.parametrize("name", ["REDLINE_ENV", "APP_ENV", "ENV", "ENVIRONMENT", "FLASK_ENV"])
@pytest.mark.parametrize("value", ["prod", "Production", " PRODUCTION "])
def test_environment_variables_mark_production(name, value):
    assert production_signals({name: value}) == [f"{name}={value.strip().lower()}"]


def test_development_values_are_not_signals():
    assert production_signals({"APP_ENV": "development", "ENV": "staging", "OTHER": "prod"}) == []
    assert production_signals({}) == []


def test_debug_false_is_a_signal_only_when_given():
    assert production_signals({}, debug=False) == ["DEBUG is False"]
    assert production_signals({}, debug=True) == []
    assert production_signals({}, debug=None) == []


def test_refuse_logs_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="redline"):
        assert refuse_in_production("flask", {"APP_ENV": "production"}) is True
    assert "not attached" in caplog.text and "REDLINE_ALLOW_PRODUCTION" in caplog.text


def test_override_attaches_with_a_warning(caplog):
    env = {"APP_ENV": "prod", "REDLINE_ALLOW_PRODUCTION": "1"}
    with caplog.at_level(logging.WARNING, logger="redline"):
        assert refuse_in_production("flask", env) is False
    assert "attached although" in caplog.text
    assert refuse_in_production("flask", {"APP_ENV": "prod", "REDLINE_ALLOW_PRODUCTION": "0"})


def test_development_does_not_log(caplog):
    with caplog.at_level(logging.WARNING, logger="redline"):
        assert refuse_in_production("flask", {}) is False
    assert caplog.text == ""


def test_new_variables_are_not_unknown_settings():
    # would raise "Unknown Redline environment variable(s)" otherwise
    parse_env({"REDLINE_ENV": "production", "REDLINE_ALLOW_PRODUCTION": "1"})
