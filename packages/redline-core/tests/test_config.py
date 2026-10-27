"""Layered configuration: defaults → adapter → preset → file → env → framework."""

from __future__ import annotations

import sys

import pytest

from redline_core.conf import FEATURES, PRESETS, Settings, SettingsError
from redline_core.config import (
    GROUPS,
    config_warnings,
    find_config_file,
    load_config_file,
    mask_secrets,
    parse_env,
    parse_env_value,
    resolve_settings,
)

TOKEN = "t" * 32
needs_toml = pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")


def resolve(framework=None, *, env=None, root=None, **kw):
    return resolve_settings(framework, environ=env or {}, project_root=root, **kw)


# ── layering & provenance ─────────────────────────────────────────────────────────────


def test_defaults_only(tmp_path):
    r = resolve(root=tmp_path)
    assert r.settings == Settings()
    assert set(r.sources.values()) == {"default"}
    assert r.file is None


def test_every_layer_and_its_source(tmp_path):
    (tmp_path / "redline.toml").write_text(
        "slow_query_ms = 7\nsample_rate = 0.3\nmax_rows = 5\n[features]\nsse = false\n"
    )
    r = resolve(
        {"sample_rate": 0.9},
        env={"REDLINE_MAX_ROWS": "50", "REDLINE_PRESET": "staging", "REDLINE_TOKEN": TOKEN},
        root=tmp_path,
        adapter_defaults={"too_many_queries": 77, "sample_rate": 0.1},
    )
    s = r.settings
    assert s.too_many_queries == 77 and r.sources["too_many_queries"] == "adapter"
    assert s.capture_locals is False and r.sources["capture_locals"] == "preset"
    assert s.access == "token" and r.sources["access"] == "preset"
    assert s.slow_query_ms == 7 and r.sources["slow_query_ms"] == "file"
    assert s.max_rows == 50 and r.sources["max_rows"] == "env"
    assert s.sample_rate == 0.9 and r.sources["sample_rate"] == "framework"
    assert s.feature("sse") is False and s.feature("explain") is True
    assert r.sources["features"] == "file"
    assert r.env_names["max_rows"] == "REDLINE_MAX_ROWS"
    assert r.file == str(tmp_path / "redline.toml")


def test_preset_only_sets_defaults(tmp_path):
    r = resolve(
        {"preset": "production", "token": TOKEN, "capture_request_body": True, "read_only": False},
        root=tmp_path,
    )
    s = r.settings
    assert s.preset == "production"
    assert s.capture_request_body is True and r.sources["capture_request_body"] == "framework"
    assert s.read_only is False
    assert s.capture_response_body is False and r.sources["capture_response_body"] == "preset"
    assert s.sample_rate == 0.05 and s.log_level == "WARNING" and s.badge is False
    assert any("capture_request_body is on" in w for w in r.warnings)


@pytest.mark.parametrize("preset", list(PRESETS))
def test_presets_resolve(tmp_path, preset):
    r = resolve({"preset": preset, "token": TOKEN}, root=tmp_path)
    for key, value in PRESETS[preset].items():
        assert getattr(r.settings, key) == value


def test_preset_from_env_or_file(tmp_path):
    (tmp_path / "redline.toml").write_text(f'preset = "staging"\ntoken = "{TOKEN}"\n')
    assert resolve(root=tmp_path).settings.sample_rate == 0.25
    r = resolve(env={"REDLINE_PRESET": "dev"}, root=tmp_path)
    assert r.settings.preset == "dev" and r.settings.sample_rate == 1.0


def test_unknown_preset(tmp_path):
    with pytest.raises(SettingsError, match="preset"):
        resolve({"preset": "prod"}, root=tmp_path)


def test_production_requires_token(tmp_path):
    with pytest.raises(SettingsError, match=r"production.*access='token'"):
        resolve({"preset": "production"}, root=tmp_path)
    with pytest.raises(SettingsError, match="at least 24"):
        resolve({"preset": "production", "token": "short-token"}, root=tmp_path)
    # dev: short tokens are allowed but warned about
    r = resolve({"access": "token", "token": "short"}, root=tmp_path)
    assert any("short" in w for w in r.warnings)


def test_native_hook_implies_framework(tmp_path):
    assert resolve(native_hook=True, root=tmp_path).settings.access == "framework"
    # a preset does not count as explicit ...
    r = resolve({"preset": "production", "token": TOKEN}, native_hook=True, root=tmp_path)
    assert r.settings.access == "framework"
    # ... but file, env and framework layers do
    r = resolve(env={"REDLINE_ACCESS": "local"}, native_hook=True, root=tmp_path)
    assert r.settings.access == "local" and r.sources["access"] == "env"
    r = resolve({"access": "local"}, native_hook=True, root=tmp_path)
    assert r.settings.access == "local"


def test_settings_instance_counts_only_non_defaults(tmp_path):
    r = resolve(
        Settings(slow_query_ms=3),
        env={"REDLINE_SAMPLE_RATE": "0.5"},
        native_hook=True,
        root=tmp_path,
    )
    assert r.settings.slow_query_ms == 3 and r.sources["slow_query_ms"] == "framework"
    assert r.settings.sample_rate == 0.5  # not explicit in the instance
    assert r.settings.access == "framework"


def test_feature_switches_merge_across_layers(tmp_path):
    (tmp_path / "redline.toml").write_text("[features]\nsse = false\n")
    r = resolve({"features": {"explain": False}}, root=tmp_path)
    assert r.settings.feature("sse") is False and r.settings.feature("explain") is False
    assert r.settings.feature("load") is True


def test_error_names_the_source(tmp_path):
    with pytest.raises(SettingsError, match="REDLINE_SAMPLE_RATE"):
        resolve(env={"REDLINE_SAMPLE_RATE": "3"}, root=tmp_path)
    (tmp_path / "redline.toml").write_text("max_rows = -1\n")
    with pytest.raises(SettingsError, match=r"redline\.toml"):
        resolve(root=tmp_path)


# ── environment ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "raw", "value"),
    [
        ("enabled", "0", False),
        ("enabled", "Yes", True),
        ("enabled", "off", False),
        ("sample_rate", "0.25", 0.25),
        ("max_body_bytes", "1_000", 1000),
        ("max_rows", "none", None),
        ("max_rows", "", None),
        ("max_rows", "12", 12),
        ("release", "null", None),
        ("release", "v1", "v1"),
        ("badge", "auto", "auto"),
        ("badge", "true", True),
        ("ignore_paths", "/a, /b,,", ["/a", "/b"]),
        ("ignore_paths", '["/a", "/b,c"]', ["/a", "/b,c"]),
        ("features", "sse=0, explain=true", {"sse": False, "explain": True}),
        ("features", '{"sse": false}', {"sse": False}),
        ("basic_users", "ann=pbkdf2_sha256$1$a$b", {"ann": "pbkdf2_sha256$1$a$b"}),
        ("access", " token ", "token"),
    ],
)
def test_env_value_parsing(name, raw, value):
    assert parse_env_value(name, raw) == value


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("enabled", "maybe"),
        ("sample_rate", "lots"),
        ("max_body_bytes", "1.5"),
        ("features", "sse"),
        ("features", "sse=perhaps"),
        ("features", "[1]"),
        ("ignore_paths", "[oops"),
    ],
)
def test_env_value_errors(name, raw):
    with pytest.raises(SettingsError):
        parse_env_value(name, raw)


def test_env_unknown_variable_and_ignored_ones():
    with pytest.raises(SettingsError, match="did you mean REDLINE_SAMPLE_RATE"):
        parse_env({"REDLINE_SAMPLE_RAT": "1"})
    values, names = parse_env(
        {
            "REDLINE_CONFIG": "x",
            "REDLINE_ALLOW_OPEN": "1",
            "REDLINE_RELEASE": "v9",
            "REDLINE_TEST_PG": "postgres://",
            "redline_lowercase": "x",
            "REDLINE_": "x",
            "OTHER": "1",
            "REDLINE_ENABLED": "false",
        }
    )
    assert values == {"enabled": False} and names == {"enabled": "REDLINE_ENABLED"}


def test_env_token_keeps_its_exact_value():
    values, _ = parse_env({"REDLINE_TOKEN": TOKEN})
    assert values["token"] == TOKEN


# ── config file ───────────────────────────────────────────────────────────────────────


@needs_toml
def test_file_discovery_order(tmp_path):
    assert find_config_file(tmp_path, {}) is None
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert find_config_file(tmp_path, {}) is None  # no [tool.redline]
    (tmp_path / "pyproject.toml").write_text("[tool.redline]\nslow_query_ms = 9\n")
    assert find_config_file(tmp_path, {}) == tmp_path / "pyproject.toml"
    assert resolve(root=tmp_path).settings.slow_query_ms == 9
    (tmp_path / "redline.toml").write_text("slow_query_ms = 8\n")
    assert find_config_file(tmp_path, {}) == tmp_path / "redline.toml"
    other = tmp_path / "elsewhere.toml"
    other.write_text("slow_query_ms = 7\n")
    assert find_config_file(tmp_path, {"REDLINE_CONFIG": str(other)}) == other
    r = resolve(env={"REDLINE_CONFIG": str(other)}, root=tmp_path)
    assert r.settings.slow_query_ms == 7 and r.file == str(other)


def test_missing_explicit_config_file(tmp_path):
    with pytest.raises(SettingsError, match="no such file"):
        resolve(env={"REDLINE_CONFIG": str(tmp_path / "nope.toml")}, root=tmp_path)


@needs_toml
def test_pyproject_subtables(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.redline.features]\nexplain = false\n[tool.redline.basic_users]\n"
    )
    r = resolve(root=tmp_path)
    assert r.settings.feature("explain") is False


@needs_toml
def test_redline_toml_may_nest_its_table(tmp_path):
    p = tmp_path / "redline.toml"
    p.write_text("[redline]\nslow_query_ms = 4\n")
    assert load_config_file(p)[0] == {"slow_query_ms": 4}
    p.write_text("[tool.redline]\nslow_query_ms = 5\n")
    assert load_config_file(p)[0] == {"slow_query_ms": 5}


@needs_toml
def test_file_errors(tmp_path):
    (tmp_path / "redline.toml").write_text("slow_qery_ms = 4\n")
    with pytest.raises(SettingsError, match="did you mean 'slow_query_ms'"):
        resolve(root=tmp_path)
    (tmp_path / "redline.toml").write_text("this is = not toml [")
    with pytest.raises(SettingsError, match="Invalid TOML"):
        resolve(root=tmp_path)


@needs_toml
def test_project_root_from_framework_picks_the_file(tmp_path, monkeypatch):
    (tmp_path / "redline.toml").write_text("slow_query_ms = 6\n")
    monkeypatch.chdir(tmp_path.parent)
    r = resolve_settings({"project_root": str(tmp_path)}, environ={})
    assert r.settings.slow_query_ms == 6
    r = resolve_settings(None, environ={}, adapter_defaults={"project_root": str(tmp_path)})
    assert r.settings.slow_query_ms == 6
    assert resolve_settings(None, environ={}, read_file=False).file is None


# ── settings validation (section 18 fields) ───────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        {"access": "everyone"},
        {"allowed_ips": ["10.0.0.0/33"]},
        {"trusted_proxies": ["proxy.local"]},
        {"access": "token"},
        {"access": "token", "token": "has space"},
        {"token": TOKEN, "token_file": "/x"},
        {"access": "basic"},
        {"access": "basic", "basic_users": {"ann": "plain-password"}},
        {"basic_users": {"a:b": "pbkdf2_sha256$1$a$b"}},
        {"access": "custom"},
        {"access_check": 5},
        {"features": {"explian": False}},
        {"features": {"sse": "no"}},
        {"features": ["sse"]},
        {"secret_key": "short"},
        {"mount_path": "/"},
        {"mount_path": "/a;b"},
        {"session_ttl_s": 0},
        {"max_login_attempts": 0},
        {"read_only": "yes"},
    ],
)
def test_invalid_access_settings(bad):
    with pytest.raises(SettingsError):
        Settings.from_mapping(bad)


def test_settings_section18_defaults_and_normalisation():
    s = Settings()
    assert s.access == "local" and s.preset == "dev" and s.read_only is False
    assert dict(s.features) == dict.fromkeys(FEATURES, True)
    assert s.session_ttl_s == 43_200 and s.max_login_attempts == 10
    s = Settings.from_mapping({"ACCESS": "LOCAL", "mount_path": "x/", "features": {"SSE": False}})
    assert s.access == "local" and s.mount_path == "/x" and not s.feature("sse")
    with pytest.raises(TypeError):
        s.features["sse"] = True  # type: ignore[index]
    assert s.to_dict()["features"]["sse"] is False
    assert s.replace(read_only=True).feature("sse") is False


def test_mask_secrets_and_public(tmp_path):
    r = resolve(
        {
            "access": "basic",
            "basic_users": {"ann": "pbkdf2_sha256$1000$YQ$Yg"},
            "secret_key": "s" * 32,
        },
        root=tmp_path,
    )
    data = r.public(["runtime note"])
    assert data["settings"]["basic_users"] == {"ann": "***"}
    assert data["settings"]["secret_key"] == "***"
    assert data["settings"]["token"] is None
    assert data["warnings"][-1] == "runtime note"
    assert set(data["groups"]) == {*GROUPS, "other"}
    every = [n for names in data["groups"].values() for n in names]
    assert sorted(every) == sorted(Settings.field_names())
    assert mask_secrets({"token": "abc"}) == {"token": "***"}


def test_public_callable_access_check(tmp_path):
    def check(info):
        return True

    r = resolve({"access": "custom", "access_check": check}, root=tmp_path)
    assert r.public()["settings"]["access_check"].endswith("check")


def test_warnings():
    assert any("open" in w for w in config_warnings(Settings(access="open")))
    assert any("allowed_ips" in w for w in config_warnings(Settings(allowed_ips=("0.0.0.0/0",))))
    assert not any(
        "allowed_ips" in w for w in config_warnings(Settings(allowed_ips=("10.1.0.0/16",)))
    )
    w = config_warnings(Settings(access="token", token=TOKEN))
    assert any("secret_key" in x for x in w)
    w = config_warnings(Settings(history_retention_days=0, max_rows=0, max_age_days=0))
    assert any("history" in x for x in w) and any("never pruned" in x for x in w)
    w = config_warnings(Settings(preset="staging", access="local"))
    assert any("access='local'" in x for x in w)
    w = config_warnings(Settings(load_from_ui=True, access="token", token=TOKEN))
    assert any("load test" in x for x in w)


def test_server_timing_auto_and_production(tmp_path):
    from redline_core.ui import server_timing_enabled

    assert Settings().server_timing == "auto"
    assert server_timing_enabled(Settings(), debug=True) is True
    assert server_timing_enabled(Settings(), debug=False) is False
    assert server_timing_enabled(Settings(server_timing=True), debug=False) is True
    assert server_timing_enabled(Settings(server_timing=False), debug=True) is False
    assert server_timing_enabled(Settings(enabled=False, server_timing=True), debug=True) is False
    assert Settings.from_mapping({"server_timing": "AUTO"}).server_timing == "auto"
    with pytest.raises(SettingsError, match="server_timing"):
        Settings.from_mapping({"server_timing": "sometimes"})
    assert parse_env_value("server_timing", "auto") == "auto"
    assert parse_env_value("server_timing", "0") is False
    r = resolve({"preset": "production", "token": TOKEN}, root=tmp_path)
    assert r.settings.server_timing is False and r.sources["server_timing"] == "preset"


@pytest.mark.parametrize("bad", ["ftp://x", "localhost:8000", "http://", "http://x/?a=1", 5])
def test_base_url_validation(bad):
    with pytest.raises(SettingsError, match="base_url"):
        Settings.from_mapping({"base_url": bad})
    assert Settings(base_url="https://app.internal:8443/root").base_url
