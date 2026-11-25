"""Remote agents: two agents, one merged run."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from redline_core import cli
from redline_core.load import engine
from redline_core.load.agent import AgentError, AgentServer, RemoteAgent, validate_worker_cfg
from redline_core.load.profile import ScenarioError
from redline_core.load.scenario import parse_scenario

TOKEN = "agent-token-123456"


def start_agent(**kw):
    server = AgentServer("127.0.0.1", 0, TOKEN, max_workers=2, **kw)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.address
    return server, f"http://{host}:{port}"


@pytest.fixture
def agents():
    started = [start_agent(), start_agent()]
    yield [url for _, url in started]
    for server, _ in started:
        server.close()


def scenario(url, **extra):
    return parse_scenario({"name": "dist", "base_url": url, "requests": [{"path": "/a"}], **extra})


# ------------------------------------------------------------------ the merged run
def test_two_agents_produce_one_merged_run_that_lines_up(http_server, agents):
    state, url = http_server
    sc = scenario(url)
    result = engine.run(
        sc,
        profile={"stages": [{"rps": 20, "duration_s": 3}, {"rps": 40, "duration_s": 3}]},
        agents=agents,
        agent_token=TOKEN,
    )
    assert result.status == "done", result.message
    assert [s.step for s in result.steps] == [1, 2]
    assert result.config["workers"] == 4  # two per agent; the controller ran none
    s1, s2 = result.steps
    # the offered load is exactly the plan across all hosts: 60 and 120 requests
    assert s1.sent == 60 and s2.sent == 120 and s1.errors == s2.errors == 0
    assert state.count == 180
    # steps line up to the second: one shared clock, step 2 starts 3 s after step 1
    assert abs((s2.started_at - s1.started_at) - 3.0) < 0.05
    assert result.summary["verdict"] == "healthy"
    assert "generator" in result.summary  # generator CPU is reported


def test_the_controller_can_run_workers_as_well(http_server, agents):
    _, url = http_server
    result = engine.run(
        scenario(url),
        profile={"mode": "constant", "rps": 30, "duration_s": 2},
        agents=agents[:1],
        agent_token=TOKEN,
        workers=1,
    )
    assert result.status == "done" and result.config["workers"] == 3
    assert result.steps[0].sent == 60


def test_identities_and_data_travel_inline_to_the_agents(http_server, agents, tmp_path):
    state, url = http_server
    users = tmp_path / "users.csv"
    users.write_text("name,header:X-Api-Key\nu1,key-1\nu2,key-2\n")
    ids = tmp_path / "ids.csv"
    ids.write_text("id\n7\n8\n")
    sc = scenario(
        url,
        identities_file=str(users),
        data=[{"name": "p", "file": str(ids)}],
        requests=[{"path": "/o/{data:p.id}"}],
    )
    result = engine.run(
        sc,
        profile={"mode": "constant", "rps": 20, "duration_s": 2},
        agents=agents,
        agent_token=TOKEN,
    )
    assert result.status == "done" and result.steps[0].errors == 0
    assert set(state.api_keys) == {"key-1", "key-2"}
    assert set(state.paths) == {"/o/7", "/o/8"}


# ------------------------------------------------------------------ refusals
def test_a_wrong_token_is_refused(http_server, agents):
    _, url = http_server
    with pytest.raises(AgentError, match="refused the token"):
        engine.run(
            scenario(url),
            profile={"mode": "constant", "rps": 5, "duration_s": 1},
            agents=agents,
            agent_token="not-the-token-123",
        )


def test_an_agent_needs_a_token_of_its_own():
    with pytest.raises(AgentError):
        AgentServer("127.0.0.1", 0, "short")
    with pytest.raises(ScenarioError, match="token"):
        engine.run(
            scenario("http://127.0.0.1:1"),
            profile={"mode": "constant", "rps": 5, "duration_s": 1},
            agents=["http://127.0.0.1:1"],
        )


def test_requests_without_the_token_are_401(agents):
    assert httpx.get(agents[0] + "/v1/info").status_code == 401
    assert (
        httpx.get(agents[0] + "/v1/info", headers={"Authorization": "Bearer x"}).status_code == 401
    )
    ok = httpx.get(agents[0] + "/v1/info", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200 and ok.json()["cpus"] == 2


def test_a_token_command_is_never_sent_to_an_agent(http_server, agents):
    _, url = http_server
    sc = scenario(url, auth={"provider": "command", "command": ["echo", "tok"]})
    result = engine.run(
        sc,
        profile={"mode": "constant", "rps": 10, "duration_s": 1},
        agents=agents,
        agent_token=TOKEN,
    )
    assert result.status == "failed" and "token command or callable" in result.message


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({"base_url": "http://169.254.169.254"}, "link-local"),
        ({"base_url": "http://metadata.google.internal"}, "metadata"),
        ({"base_url": "ftp://x"}, "http(s)"),
        ({"auth": {"provider": "command"}}, "no command"),
        ({"auth": {"provider": "python"}}, "no command"),
        ({"identities_file": "/etc/passwd"}, "inline"),
        ({"requests": [{"method": "GET", "path": "//evil/x"}]}, "single '/'"),
        ({"requests": [{"method": "TELEPORT", "path": "/x"}]}, "method"),
        ({"worker": 5, "workers": 2}, "out of range"),
        ({"_crash_after_s": 1}, "_crash_after_s"),
    ],
)
def test_a_hostile_worker_config_is_refused(patch, expected):
    cfg = {
        "worker": 0, "workers": 1, "connections": 8, "max_inflight": 8,
        "base_url": "http://app.internal", "requests": [{"method": "GET", "path": "/a"}],
    }  # fmt: skip
    cfg.update(patch)
    assert expected in (validate_worker_cfg(cfg, ()) or "")


def test_allowed_targets_restrict_where_load_goes(http_server):
    _, url = http_server
    server, agent_url = start_agent(allow_targets=("https://staging.example.com",))
    try:
        result = engine.run(
            scenario(url),
            profile={"mode": "constant", "rps": 5, "duration_s": 1},
            agents=[agent_url],
            agent_token=TOKEN,
        )
        assert result.status == "failed" and "allowed targets" in result.message
    finally:
        server.close()


def test_an_agent_that_goes_away_fails_the_run_clearly(http_server):
    _, url = http_server
    server, agent_url = start_agent()

    def stop_soon():
        import time

        time.sleep(1.5)
        server.token = "rotated-away-token"  # from now on every poll is refused

    threading.Thread(target=stop_soon, daemon=True).start()
    result = engine.run(
        scenario(url),
        profile={"mode": "constant", "rps": 10, "duration_s": 8},
        agents=[agent_url],
        agent_token=TOKEN,
    )
    assert result.status == "failed" and "agent lost" in result.message
    server.stop_run()


def test_a_stop_reaches_the_agents(http_server, agents):
    _, url = http_server
    stop = threading.Event()
    threading.Timer(1.5, stop.set).start()
    result = engine.run(
        scenario(url),
        profile={"mode": "constant", "rps": 10, "duration_s": 30},
        agents=agents,
        agent_token=TOKEN,
        stop_event=stop,
    )
    assert result.status == "stopped"


# ------------------------------------------------------------------ generator cpu and CLI
def test_generator_limited_steps_are_flagged():
    from redline_core.load.analysis import StepResult, analyze

    steps = [
        StepResult(step=1, target_rps=10, started_at=0, ended_at=10, duration_s=10,
                   ok=100, sent=100, generator_cpu_pct=40.0),
        StepResult(step=2, target_rps=20, started_at=10, ended_at=20, duration_s=10,
                   ok=200, sent=200, generator_cpu_pct=97.0),
    ]  # fmt: skip
    assert steps[1].to_dict()["generator_limited"] is True
    assert steps[0].to_dict()["generator_limited"] is False
    summary = analyze(steps)
    assert summary["generator"] == {"max_cpu_pct": 97.0, "limited_steps": [2]}


def test_cli_agents_flag(http_server, agents, tmp_path, capsys, monkeypatch):
    _, url = http_server
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"name": "c", "base_url": url, "requests": [{"path": "/a"}]}))
    token_file = tmp_path / "agent-token"
    token_file.write_text(TOKEN + "\n")
    code = cli.main(
        ["load", str(path), "--no-store", "--rps", "20", "--duration", "2s", "-q",
         "--agents", ",".join(agents), "--agent-token-file", str(token_file)]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0 and "total: sent 40" in out
    assert cli.main(["load", str(path), "--no-store", "--agents", agents[0]]) == 2  # no token


def test_remote_agent_measures_the_clock_offset(agents):
    handle = RemoteAgent(agents[0], TOKEN)
    handle.info()
    assert abs(handle.clock_offset) < 0.5 and handle.cpus == 2
    handle.close()
