"""Identities, data feeds and token refresh (scenario keys that vary the offered load)."""

from __future__ import annotations

import json
import random
import sys
from collections import Counter

import pytest

from redline_core.load import engine
from redline_core.load.profile import ScenarioError
from redline_core.load.scenario import parse_scenario, render_path
from redline_core.load.sinks import redact_config
from redline_core.load.variation import (
    DataFeed,
    DataSource,
    Identity,
    IdentityPicker,
    identity_tag,
    load_identities,
)


def base(url="http://127.0.0.1:1", **extra):
    return {"name": "v", "base_url": url, "requests": [{"path": "/a"}], **extra}


# ------------------------------------------------------------------------ parsing
def test_existing_scenarios_are_unchanged():
    sc = parse_scenario(base())
    assert sc.identities == () and sc.data == () and sc.auth is None
    assert "identities" not in sc.to_dict() and "auth" not in sc.to_dict()


def test_identity_validation():
    ok = parse_scenario(base(identities=[{"name": "u1", "headers": {"X-Api-Key": "k"}}]))
    assert ok.identity_mode == "round_robin"
    for bad in (
        [{"headers": {}}],
        [{"name": "bad name"}],
        [{"name": "u", "headers": {"Bad Name": "x"}}],
        [{"name": "u", "headers": {"X": "a\nb"}}],
        [],
    ):
        with pytest.raises(ScenarioError):
            parse_scenario(base(identities=bad))
    with pytest.raises(ScenarioError, match="identity_mode"):
        parse_scenario(base(identities=[{"name": "u"}], identity_mode="nope"))


def test_identities_from_env_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_ONE", "secret-one")
    csv_file = tmp_path / "users.csv"
    csv_file.write_text("name,header:X-Api-Key,cookie:session\nu2,key2,sess2\nu3,key3,sess3\n")
    ids = load_identities(
        [{"name": "u1", "headers": {"X-Api-Key": "${TOKEN_ONE}"}}], "users.csv", str(tmp_path)
    )
    assert [i.name for i in ids] == ["u1", "u2", "u3"]
    assert ids[0].headers["X-Api-Key"] == "secret-one"
    assert ids[1].request_headers() == {"X-Api-Key": "key2", "Cookie": "session=sess2"}
    monkeypatch.delenv("TOKEN_ONE")
    with pytest.raises(ScenarioError) as info:
        load_identities([{"name": "u1", "headers": {"X-Api-Key": "${TOKEN_ONE}"}}], None)
    assert "TOKEN_ONE" in str(info.value)


def test_duplicate_identity_names_are_refused():
    with pytest.raises(ScenarioError, match="unique"):
        load_identities([{"name": "a"}, {"name": "a"}], None)


def test_identity_tag_is_header_safe():
    assert identity_tag("a b;c=d") == "a_b_c_d"


def test_data_source_must_be_declared_and_columns_exist(tmp_path):
    (tmp_path / "ids.csv").write_text("id,slug\n1,a\n2,b\n")
    with pytest.raises(ScenarioError, match="unknown data source"):
        parse_scenario(base(requests=[{"path": "/o/{data:orders.id}"}]))
    sc = parse_scenario(
        base(
            data=[{"name": "orders", "file": str(tmp_path / "ids.csv")}],
            requests=[{"path": "/o/{data:orders.id}"}],
        )
    )
    with pytest.raises(ScenarioError, match="no column"):
        engine._preflight(
            sc.replace(
                requests=(
                    parse_scenario(
                        base(
                            data=[{"name": "orders", "file": str(tmp_path / "ids.csv")}],
                            requests=[{"path": "/o/{data:orders.nope}"}],
                        )
                    ).requests[0],
                )
            )
        )


# ------------------------------------------------------------------------ pickers
def ids(n):
    return [Identity(name=f"u{i}") for i in range(n)]


def test_round_robin_across_workers_covers_every_identity_evenly():
    counts = Counter()
    pickers = [IdentityPicker(ids(50), "round_robin", k, 4) for k in range(4)]
    for _ in range(500):
        for p in pickers:
            counts[p.pick(random.Random(0)).name] += 1
    assert len(counts) == 50
    assert max(counts.values()) == min(counts.values()) == 40


def test_random_mode_is_seeded():
    a = IdentityPicker(ids(20), "random", 0, 1)
    b = IdentityPicker(ids(20), "random", 0, 1)
    ra, rb = random.Random(7), random.Random(7)
    assert [a.pick(ra).name for _ in range(30)] == [b.pick(rb).name for _ in range(30)]


def test_sticky_mode_keeps_one_identity_per_worker():
    p = IdentityPicker(ids(5), "sticky", 3, 8)
    assert {p.pick(random.Random(0)).name for _ in range(20)} == {"u3"}


# ------------------------------------------------------------------------ data feeds
def rows_file(tmp_path, n=1000):
    path = tmp_path / "rows.csv"
    path.write_text("id,slug\n" + "".join(f"{i},s{i}\n" for i in range(n)))
    return path


def test_zipf_is_skewed_and_reproducible(tmp_path):
    src = DataSource(name="r", file=str(rows_file(tmp_path)), pick="zipf", zipf_s=1.2)

    def draw(seed):
        feed = DataFeed([src], 0, 1)
        rng = random.Random(seed)
        return [feed.new_context(rng)("r", "id") for _ in range(5000)]

    first = draw(3)
    assert first == draw(3)
    counts = Counter(first)
    median = sorted(counts.values())[len(counts) // 2]
    assert counts.most_common(1)[0][1] > 10 * median


def test_one_row_serves_every_reference_in_a_request(tmp_path):
    src = DataSource(name="r", file=str(rows_file(tmp_path)), pick="random")
    feed = DataFeed([src], 0, 1)
    rng = random.Random(1)
    for _ in range(50):
        look = feed.new_context(rng)
        i, slug = look("r", "id"), look("r", "slug")
        assert slug == f"s{i}"


def test_sequential_walks_the_file_across_workers(tmp_path):
    src = DataSource(name="r", file=str(rows_file(tmp_path, 6)), pick="sequential")
    seen = []
    feeds = [DataFeed([src], k, 2) for k in range(2)]
    for _ in range(3):
        for feed in feeds:
            seen.append(feed.new_context(random.Random(0))("r", "id"))
    assert sorted(seen) == ["0", "1", "2", "3", "4", "5"]


def test_render_path_resolves_data_only_with_a_lookup():
    assert render_path("/a/{data:r.id}", random.Random(0), lambda s, c: "42") == "/a/42"
    with pytest.raises(ValueError):
        render_path("/a/{data:r.id}", random.Random(0))


# ------------------------------------------------------------------------ redaction
def test_the_stored_config_holds_no_identity_or_auth_secrets():
    config = parse_scenario(
        base(
            identities=[
                {"name": "u1", "headers": {"X-Api-Key": "sk-live-123"}, "cookies": {"s": "cook-9"}}
            ],
            auth={
                "provider": "command",
                "command": ["get-token", "--password", "pw-777"],
            },
        )
    ).to_dict()
    text = json.dumps(redact_config(config))
    for secret in ("sk-live-123", "cook-9", "pw-777"):
        assert secret not in text
    assert "u1" in text and "get-token" in text


# ------------------------------------------------------------------------ end to end
def run_scenario(url, data, rps=40, duration=3, **kw):
    sc = parse_scenario({**data, "base_url": url})
    return engine.run(
        sc, profile={"mode": "constant", "rps": rps, "duration_s": duration}, workers=2, **kw
    )


def test_fifty_identities_get_equal_shares_and_no_secret_is_stored(http_server):
    state, url = http_server
    identities = [
        {"name": f"user{i}", "headers": {"X-Api-Key": f"sk-secret-{i}"}} for i in range(50)
    ]
    result = run_scenario(
        url,
        {"name": "ids", "requests": [{"path": "/a"}], "identities": identities},
        rps=100,
        duration=5,
    )
    assert result.status == "done"
    used = Counter(h.split("identity=")[1] for h in state.headers if "identity=" in h)
    assert len(used) == 50
    assert max(used.values()) - min(used.values()) <= 4  # round robin: near-equal shares
    assert set(state.api_keys) >= {f"sk-secret-{i}" for i in range(50)}
    assert result.summary["identities"]["count"] == 50
    assert "sk-secret" not in json.dumps(result.config) + json.dumps(result.summary)


def test_cookies_and_request_headers_override_identity_headers(http_server):
    state, url = http_server
    data = {
        "name": "c",
        "headers": {"X-Api-Key": "scenario"},
        "requests": [{"path": "/a", "headers": {"X-Api-Key": "request"}}],
        "identities": [
            {"name": "u", "headers": {"X-Api-Key": "identity"}, "cookies": {"sid": "abc"}}
        ],
    }
    run_scenario(url, data, rps=10, duration=2)
    assert set(state.api_keys) == {"request"}
    assert set(state.cookies) == {"sid=abc"}


def test_data_feed_fills_path_and_json_body(http_server, tmp_path):
    state, url = http_server
    path = tmp_path / "d.csv"
    path.write_text("id,sku\n7,A-7\n8,A-8\n")
    data = {
        "name": "d",
        "data": [{"name": "p", "file": str(path), "pick": "sequential"}],
        "requests": [{"method": "POST", "path": "/p/{data:p.id}", "json": {"sku": "{data:p.sku}"}}],
    }
    run_scenario(url, data, rps=20, duration=2)
    assert set(state.paths) == {"/p/7", "/p/8"}
    bodies = {json.loads(b)["sku"] for b in state.bodies}
    assert bodies == {"A-7", "A-8"}


def test_missing_data_file_fails_before_any_request(http_server, tmp_path):
    state, url = http_server
    data = {
        "name": "d",
        "data": [{"name": "p", "file": str(tmp_path / "gone.csv")}],
        "requests": [{"path": "/p/{data:p.id}"}],
    }
    with pytest.raises(ScenarioError):
        run_scenario(url, data, rps=5, duration=1)
    assert state.count == 0


# ------------------------------------------------------------------------ auth
TOKEN_SCRIPT = (
    "import sys, pathlib\n"
    "p = pathlib.Path(sys.argv[1])\n"
    "n = int(p.read_text() or 0) + 1 if p.exists() else 1\n"
    "p.write_text(str(n))\n"
    "print('tok-%d' % n)\n"
)


def test_token_is_refreshed_before_expiry_during_a_long_run(http_server, tmp_path):
    state, url = http_server
    script = tmp_path / "tok.py"
    script.write_text(TOKEN_SCRIPT)
    data = {
        "name": "a",
        "requests": [{"path": "/a"}],
        "auth": {
            "provider": "command",
            "command": [sys.executable, str(script), str(tmp_path / "count")],
            "ttl_s": 2,
            "refresh_before_s": 1,
        },
    }
    result = run_scenario(url, data, rps=20, duration=7)
    assert result.status == "done"
    tokens = {a for a in state.authorizations if a}
    assert len(tokens) >= 3  # refreshed more than once (every ~1 s per worker)
    assert all(a.startswith("Bearer tok-") for a in state.authorizations)
    assert result.steps[0].errors == 0


def test_a_failing_token_provider_stops_the_run_without_leaking(http_server, tmp_path):
    state, url = http_server
    data = {
        "name": "a",
        "requests": [{"path": "/a"}],
        "auth": {
            "provider": "command",
            "command": [sys.executable, "-c", "import sys; sys.exit(3)"],
        },
    }
    result = run_scenario(url, data, rps=5, duration=2)
    assert result.status == "failed"
    assert "could not get an auth token" in result.message
    assert state.count == 0


def test_request_provider_extracts_the_token(http_server):
    _state, url = http_server
    data = {
        "name": "a",
        "requests": [{"path": "/a"}],
        "auth": {
            "provider": "request",
            "request": {"method": "GET", "path": "/token"},
            "extract": "ok",
        },
    }
    # the test server answers {"ok": true}: a non-string token is refused clearly
    result = run_scenario(url, data, rps=5, duration=1)
    assert result.status == "failed"


# ------------------------------------------------------------------------ init and the UI
def test_the_ui_cannot_set_scenario_keys_that_run_code_or_read_files(tmp_path):
    from redline_core.api import RedlineAPI
    from redline_core.conf import Settings
    from redline_core.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "u.db", background=False)
    api = RedlineAPI(store, Settings(load_from_ui=True), run_launcher=lambda *a: "r1")
    body = {
        "name": "x",
        "requests": [{"path": "/"}],
        "profile": {"mode": "constant", "rps": 1, "duration_s": 1},
    }
    assert api.dispatch("POST", "api/runs", body=body, base_url="http://127.0.0.1:8000")[0] == 202
    for key, value in (
        ("auth", {"provider": "command", "command": ["sh", "-c", "id"]}),
        ("identities_file", "/etc/passwd"),
        ("data", [{"name": "p", "file": "/etc/passwd"}]),
        ("identities", [{"name": "u"}]),
    ):
        status, payload = api.dispatch(
            "POST", "api/runs", body={**body, key: value}, base_url="http://127.0.0.1:8000"
        )
        assert status == 400 and key in payload["error"]["message"]
    store.close()


def test_load_init_writes_data_files_and_identity_keys(tmp_path, capsys):
    import os

    from redline_core import cli
    from redline_core.storage import SQLiteStore

    from ..conftest import make_trace

    db = str(tmp_path / "t.db")
    store = SQLiteStore(db, background=False)
    for i in range(6):
        store.save(make_trace(path=f"/orders/{i % 3}", route="/orders/<id>", method="GET"))
    store.flush()
    store.close()
    out = tmp_path / "sc.toml"
    code = cli.main(
        [
            "load", "init", "--from-traces", "--store", db, "-o", str(out),
            "--data-dir", str(tmp_path / "data"),
            "--identities-file", "users.csv", "--identity-mode", "random",
        ]
    )  # fmt: skip
    assert code == 0
    text = out.read_text()
    assert "[[data]]" in text and 'pick = "zipf"' in text
    assert "{data:ep1.path}" in text
    assert 'identities_file = "users.csv"' in text and 'identity_mode = "random"' in text
    rows = (tmp_path / "data" / "ep1.csv").read_text().split()
    assert rows[0] == "path" and len(rows) == 4  # header + 3 distinct paths
    assert os.path.exists(out)
