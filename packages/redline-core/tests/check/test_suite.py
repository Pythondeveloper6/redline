from __future__ import annotations

import json
import sys

import pytest

from redline_core.check.suite import (
    ANY_INDEX,
    ANY_KEY,
    Suite,
    SuiteError,
    expand_env,
    format_path,
    parse_path,
    path_matches,
    redact_suite,
)


def suite(**overrides):
    data = {"name": "s", "base_url": "http://127.0.0.1:8000", "requests": [{"path": "/a"}]}
    data.update(overrides)
    return data


def test_defaults_and_names():
    s = Suite.from_dict(suite())
    assert s.repeat == 20 and s.warmup == 3 and s.timeout_s == 10.0
    assert s.thresholds.slower_pct == 10 and s.thresholds.slower_ms == 2
    assert s.thresholds.max_query_increase == 0
    r = s.requests[0]
    assert (r.name, r.method, r.perf, r.response) == ("GET /a", "GET", True, True)


@pytest.mark.parametrize("method", ["POST", "put", "PATCH", "DELETE"])
def test_write_methods_refused(method):
    with pytest.raises(SuiteError) as exc:
        Suite.from_dict(suite(requests=[{"name": "w", "path": "/a", "method": method}]))
    msg = str(exc.value)
    assert "requests[0].method" in msg and "'w'" in msg
    assert "read-only" in msg and method.upper() in msg


def test_head_and_options_allowed():
    s = Suite.from_dict(
        suite(requests=[{"path": "/a", "method": "head"}, {"path": "/a", "method": "OPTIONS"}])
    )
    assert [r.method for r in s.requests] == ["HEAD", "OPTIONS"]


def test_unknown_keys_with_hint():
    with pytest.raises(SuiteError, match=r"repeats: unknown key; did you mean 'repeat'"):
        Suite.from_dict(suite(repeats=3))
    with pytest.raises(SuiteError, match=r"requests\[0\]\.ignores.*'ignore'"):
        Suite.from_dict(suite(requests=[{"path": "/a", "ignores": []}]))
    with pytest.raises(SuiteError, match=r"thresholds\.slower"):
        Suite.from_dict(suite(thresholds={"slower": 1}))


@pytest.mark.parametrize(
    "path", ["http://evil.example/a", "https://x/y", "//evil.example/a", "javascript:alert(1)"]
)
def test_absolute_urls_refused(path):
    with pytest.raises(SuiteError, match="absolute URLs are refused"):
        Suite.from_dict(suite(requests=[{"path": path}]))


def test_relative_path_and_whitespace_refused():
    with pytest.raises(SuiteError, match="must start with '/'"):
        Suite.from_dict(suite(requests=[{"path": "orders"}]))
    with pytest.raises(SuiteError, match="whitespace"):
        Suite.from_dict(suite(requests=[{"path": "/a b"}]))


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("repeat", 0, "repeat"),
        ("repeat", 1001, "repeat"),
        ("repeat", 2.5, "integer"),
        ("warmup", -1, "warmup"),
        ("warmup", 101, "warmup"),
        ("timeout_s", 0, "timeout_s"),
        ("base_url", "ftp://x", "base_url"),
        ("name", "", "name"),
        ("requests", [], "non-empty"),
        ("headers", {"bad name": "x"}, "invalid header name"),
        ("headers", {"X-A": "${env:not valid}"}, "bad placeholder"),
        ("thresholds", {"slower_pct": -1}, "slower_pct"),
        ("thresholds", {"max_query_increase": 0.5}, "max_query_increase"),
    ],
)
def test_bad_values(key, value, match):
    with pytest.raises(SuiteError, match=match):
        Suite.from_dict(suite(**{key: value}))


def test_request_level_errors():
    dup = [{"name": "x", "path": "/a"}, {"name": "x", "path": "/b"}]
    with pytest.raises(SuiteError, match=r"requests\[1\]\.name \('x'\): duplicate"):
        Suite.from_dict(suite(requests=dup))
    with pytest.raises(SuiteError, match="nothing to check"):
        Suite.from_dict(suite(requests=[{"path": "/a", "perf": False, "response": False}]))
    with pytest.raises(SuiteError, match=r"ignore\[0\].*bad JSON path"):
        Suite.from_dict(suite(requests=[{"path": "/a", "ignore": ["a..b"]}]))
    with pytest.raises(SuiteError, match="expect_status"):
        Suite.from_dict(suite(requests=[{"path": "/a", "expect_status": 99}]))
    with pytest.raises(SuiteError, match="true or false"):
        Suite.from_dict(suite(requests=[{"path": "/a", "perf": "yes"}]))


def test_expect_status_forms():
    s = Suite.from_dict(
        suite(
            requests=[
                {"path": "/a", "expect_status": 200},
                {"path": "/b", "expect_status": [200, 304]},
            ]
        )
    )
    assert s.requests[0].expect_status == (200,)
    assert s.requests[1].expect_status == (200, 304)
    assert s.to_dict()["requests"][0]["expect_status"] == 200


def test_file_errors_name_the_file(tmp_path):
    p = tmp_path / "suite.json"
    p.write_text(json.dumps(suite(requests=[{"name": "list", "path": "/a", "method": "POST"}])))
    with pytest.raises(SuiteError) as exc:
        Suite.from_file(p)
    assert str(exc.value).startswith(f"{p}: requests[0].method ('list'): ")
    with pytest.raises(SuiteError, match="file not found"):
        Suite.from_file(tmp_path / "missing.toml")
    bad = tmp_path / "s.yaml"
    bad.write_text("x")
    with pytest.raises(SuiteError, match="unsupported suite type"):
        Suite.from_file(bad)
    broken = tmp_path / "b.json"
    broken.write_text("{")
    with pytest.raises(SuiteError, match="invalid JSON"):
        Suite.from_file(broken)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_toml_load_and_round_trip(tmp_path):
    p = tmp_path / "orders.toml"
    p.write_text(
        """
name = "orders refactor"
base_url = "http://127.0.0.1:8000/"
headers = { Authorization = "Bearer ${env:API_TOKEN}" }
timeout_s = 5
repeat = 7
warmup = 1
[thresholds]
slower_pct = 15
slower_ms = 1.5
max_query_increase = 2

[[requests]]
name = "list orders"
path = "/orders?page=1"
ignore = ["data[*].created_at", "meta.request_id", '["a.b"].c']
ignore_headers = ["Vary"]
expect_status = 200

[[requests]]
path = "/health"
perf = false
"""
    )
    s = Suite.from_file(p)
    assert s.base_url == "http://127.0.0.1:8000"
    assert s.source == str(p)
    assert s.thresholds.slower_pct == 15 and s.thresholds.max_query_increase == 2
    assert s.requests[0].ignore_headers == ("vary",)
    assert s.requests[1].name == "GET /health" and not s.requests[1].perf
    out = tmp_path / "again.toml"
    out.write_text(s.to_toml())
    again = Suite.from_file(out)
    assert again.to_dict() == {**s.to_dict(), "name": "orders refactor"}
    assert "${env:API_TOKEN}" in s.to_toml()
    assert json.loads(s.to_json()) == s.to_dict()


def test_invalid_toml(tmp_path):
    if sys.version_info < (3, 11):
        pytest.skip("tomllib")
    p = tmp_path / "x.toml"
    p.write_text("name = ")
    with pytest.raises(SuiteError, match="invalid TOML"):
        Suite.from_file(p)


# ----------------------------------------------------------------------- env placeholders
def test_env_expansion_at_run_time_only():
    s = Suite.from_dict(
        suite(
            headers={"Authorization": "Bearer ${env:API_TOKEN}", "Accept": "application/json"},
            requests=[{"path": "/a", "headers": {"accept": "text/html", "X-T": "${env:T}-x"}}],
        )
    )
    env = {"API_TOKEN": "s3cret", "T": "t"}
    assert s.headers_for(s.requests[0], env) == {
        "Authorization": "Bearer s3cret",
        "accept": "text/html",  # request headers win, case-insensitively
        "X-T": "t-x",
    }
    assert "s3cret" not in json.dumps(s.to_dict()) and "s3cret" not in s.to_toml()
    assert s.headers["Authorization"] == "Bearer ${env:API_TOKEN}"
    assert s.env_names() == ["API_TOKEN", "T"]
    with pytest.raises(SuiteError, match="API_TOKEN is not set"):
        s.headers_for(s.requests[0], {"T": "t"})


def test_expand_env_plain():
    assert expand_env("no placeholders", {}) == "no placeholders"
    assert expand_env("${env:A}${env:B}", {"A": "1", "B": "2"}) == "12"


def test_redact_suite_keeps_placeholders_masks_literals():
    s = Suite.from_dict(
        suite(
            base_url="http://user:pw@127.0.0.1:8000",
            headers={"Authorization": "Bearer ${env:API_TOKEN}", "X-Api-Key": "literal"},
            requests=[
                {"path": "/a?token=abc", "headers": {"Cookie": "sessionid=xyz"}},
                {"path": "/b", "headers": {"Cookie": "${env:COOKIE}"}},
            ],
        )
    )
    out = redact_suite(s)
    assert out["headers"]["Authorization"] == "Bearer ${env:API_TOKEN}"
    assert out["headers"]["X-Api-Key"] == "[redacted]"
    assert "pw" not in out["base_url"]
    assert "abc" not in out["requests"][0]["path"]
    assert out["requests"][0]["headers"]["Cookie"] == "[redacted]"
    assert out["requests"][1]["headers"]["Cookie"] == "${env:COOKIE}"
    assert "xyz" not in json.dumps(out) and "literal" not in json.dumps(out)


# ----------------------------------------------------------------------- JSON paths
@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("$", ()),
        ("a", ("a",)),
        ("a.b.c", ("a", "b", "c")),
        ("data[0].id", ("data", 0, "id")),
        ("data[*].created_at", ("data", ANY_INDEX, "created_at")),
        ("*.id", (ANY_KEY, "id")),
        ("meta.*", ("meta", ANY_KEY)),
        ('["a.b"].c', ("a.b", "c")),
        ('x["a[0]"]', ("x", "a[0]")),
        ('["*"]', ("*",)),
        ("[0][1]", (0, 1)),
        ("$.a[2]", ("a", 2)),
        ("$[3]", (3,)),
    ],
)
def test_parse_path(text, tokens):
    assert parse_path(text) == tokens


@pytest.mark.parametrize("bad", ["", ".a", "a..b", "a[", "a[x]", 'a["b"', "a]b", "a[0]b", 'a"b'])
def test_parse_path_errors(bad):
    with pytest.raises(ValueError):
        parse_path(bad)


@pytest.mark.parametrize(
    "tokens",
    [(), ("a",), ("a", 0, "b"), ("a.b", "c"), ("*",), ("with space",), ("$",), (0,), ("", "x")],
)
def test_format_round_trip(tokens):
    assert parse_path(format_path(tokens)) == tokens


def test_format_wildcards():
    assert format_path(("data", ANY_INDEX, ANY_KEY)) == "data[*].*"
    assert format_path((ANY_KEY, "id")) == "*.id"


def test_path_matches():
    rule = parse_path("data[*].created_at")
    assert path_matches(rule, ("data", 3, "created_at"))
    assert path_matches(rule, ("data", 3, "created_at", "tz"))  # subtree
    assert not path_matches(rule, ("data", "3", "created_at"))
    assert not path_matches(rule, ("data", 3))
    assert path_matches(parse_path("*.id"), ("user", "id"))
    assert not path_matches(parse_path("*.id"), (0, "id"))
    assert path_matches(parse_path("$"), ("anything",))
    assert not path_matches(parse_path("a"), ("b",))
