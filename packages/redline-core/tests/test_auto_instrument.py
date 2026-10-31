from __future__ import annotations

import logging

import pytest

from redline_core import Settings
from redline_core.auto_instrument import auto_instrument
from redline_core.conf import SettingsError


def _status(**state):
    return [
        {"name": name, "installed": installed, "module_loaded": False, "instrumented": active}
        for name, (installed, active) in state.items()
    ]


@pytest.fixture(autouse=True)
def _reset():
    from redline_core import auto_instrument as mod

    mod._warned.clear()
    yield
    mod._warned.clear()


def test_default_is_warn_and_values_are_validated():
    assert Settings().auto_instrument == "warn"
    assert Settings(auto_instrument=" ON ").auto_instrument == "on"
    assert Settings(auto_instrument=True).auto_instrument == "on"
    assert Settings(auto_instrument=False).auto_instrument == "off"
    with pytest.raises(SettingsError, match="auto_instrument"):
        Settings(auto_instrument="sometimes")


def test_warn_logs_exactly_one_warning_listing_missing_integrations(monkeypatch, caplog):
    from redline_core import auto_instrument as mod

    monkeypatch.setattr(
        mod,
        "integration_status",
        lambda: _status(
            httpx=(True, False),
            requests=(True, True),
            redis=(False, False),
            boto3=(True, False),
            asyncpg=(True, False),
        ),
    )
    with caplog.at_level(logging.WARNING, logger="redline"):
        assert auto_instrument(Settings()) == []
        auto_instrument(Settings())  # the same set is not repeated
    records = [r for r in caplog.records if "not instrumented" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "httpx, boto3" in message
    assert "requests" not in message and "redis" not in message and "asyncpg" not in message
    assert 'auto_instrument="on"' in message


def test_off_and_disabled_are_silent(monkeypatch, caplog):
    from redline_core import auto_instrument as mod

    monkeypatch.setattr(mod, "integration_status", lambda: _status(httpx=(True, False)))
    with caplog.at_level(logging.WARNING, logger="redline"):
        auto_instrument(Settings(auto_instrument="off"))
        auto_instrument(Settings(enabled=False))
    assert not caplog.records


def test_on_instruments_each_installed_integration_and_survives_failures(monkeypatch, caplog):
    from redline_core import auto_instrument as mod

    monkeypatch.setattr(
        mod,
        "integration_status",
        lambda: _status(httpx=(True, False), redis=(True, False), boto3=(False, False)),
    )
    called = []

    def fake(name):
        called.append(name)
        if name == "redis":
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(mod, "_instrument", fake)
    with caplog.at_level(logging.WARNING, logger="redline"):
        assert auto_instrument(Settings(auto_instrument="on")) == ["httpx"]
    assert called == ["httpx", "redis"]
    assert any("could not instrument redis" in r.getMessage() for r in caplog.records)


def test_on_really_patches_httpx_and_status_reports_it():
    pytest.importorskip("httpx")
    from redline_core.contrib.httpx import uninstrument_httpx
    from redline_core.integration_status import integration_status

    try:
        assert "httpx" in auto_instrument(Settings(auto_instrument="on"))
        entry = next(i for i in integration_status() if i["name"] == "httpx")
        assert entry["instrumented"] is True
        assert "httpx" not in auto_instrument(Settings(auto_instrument="on"))
    finally:
        for name in ("httpx", "requests", "redis", "boto3"):
            try:
                getattr(
                    __import__(f"redline_core.contrib.{name}", fromlist=["x"]),
                    f"uninstrument_{name}",
                )()
            except Exception:
                pass
        uninstrument_httpx()


def test_fastapi_install_applies_the_setting(monkeypatch, tmp_path):
    pytest.importorskip("fastapi_redline")
    from fastapi import FastAPI

    from fastapi_redline import install

    seen = []
    monkeypatch.setattr(
        "fastapi_redline.integration.auto_instrument", lambda s: seen.append(s.auto_instrument)
    )
    install(
        FastAPI(),
        settings={"storage_path": str(tmp_path / "r.db"), "auto_instrument": "off"},
    )
    assert seen == ["off"]
