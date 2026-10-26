from __future__ import annotations

import pytest

from redline_core.conf import Settings, SettingsError


def test_defaults_match_contract():
    s = Settings()
    assert s.enabled is True
    assert s.storage_path == ".redline/redline.db"
    assert s.sample_rate == 1.0
    assert s.always_keep_slower_than_ms == 500
    assert s.n_plus_one_threshold == 5
    assert s.slow_query_ms == 100
    assert s.too_many_queries == 50
    assert s.profile == "opt_in"
    assert s.max_rows == 10_000 and s.max_age_days == 7
    assert "authorization" in s.redact_headers
    assert s.max_body_bytes == 20_000
    assert s.explain_analyze is False


def test_from_mapping_is_case_insensitive_and_coerces_lists():
    s = Settings.from_mapping({"SAMPLE_RATE": 0.5, "ignore_paths": ["/health"]})
    assert s.sample_rate == 0.5
    assert s.ignore_paths == ("/health",)
    assert s.to_dict()["ignore_paths"] == ["/health"]


def test_unknown_key_has_suggestion():
    with pytest.raises(SettingsError, match="did you mean 'sample_rate'"):
        Settings.from_mapping({"sample_rte": 1})


@pytest.mark.parametrize(
    "bad",
    [
        {"sample_rate": 2},
        {"sample_rate": "x"},
        {"enabled": "yes"},
        {"profile": "sometimes"},
        {"n_plus_one_threshold": 1},
        {"max_rows": -1},
        {"redact_keys": ["("]},
        {"ignore_paths": 5},
    ],
)
def test_invalid_values(bad):
    with pytest.raises(SettingsError):
        Settings.from_mapping(bad)


def test_replace_and_direct_list():
    s = Settings(ignore_paths=["/a"])  # type: ignore[arg-type]
    assert s.ignore_paths == ("/a",)
    assert s.replace(slow_query_ms=5).slow_query_ms == 5
    assert Settings().public_dict()["thresholds"]["n_plus_one"] == 5
