from __future__ import annotations

import time


def _fill(env, n: int = 4):
    c = env.client()
    for i in range(n):
        c.get(f"/orders/{i + 1}")
    env.redline.store.flush(5)
    assert env.redline.store.count() == n


def run(env, *args, **kw):
    return env.app.test_cli_runner().invoke(args=["redline", *args], **kw)


def test_group_is_registered(make_env):
    env = make_env()
    r = env.app.test_cli_runner().invoke(args=["redline", "--help"])
    assert r.exit_code == 0
    assert "clear" in r.output and "prune" in r.output


def test_clear_asks_then_deletes(make_env):
    env = make_env()
    _fill(env)
    declined = run(env, "clear", input="n\n")
    assert declined.exit_code == 0 and "Cancelled." in declined.output
    assert env.redline.store.count() == 4
    r = run(env, "clear", input="y\n")
    assert r.exit_code == 0 and "Deleted 4 trace(s)." in r.output
    assert env.redline.store.count() == 0


def test_clear_yes_skips_the_prompt(make_env):
    env = make_env()
    _fill(env, 3)
    r = run(env, "clear", "--yes")
    assert r.exit_code == 0 and "Deleted 3 trace(s)." in r.output
    _fill(env, 2)
    assert run(env, "clear", "--noinput").exit_code == 0
    assert env.redline.store.count() == 0


def test_prune_max_rows(make_env):
    env = make_env()
    _fill(env, 5)
    r = run(env, "prune", "--max-rows", "2")
    assert r.exit_code == 0, r.output
    assert "Pruned 3 trace(s); 2 left." in r.output
    assert env.redline.store.count() == 2


def test_prune_max_age_days(make_env):
    env = make_env()
    _fill(env, 3)
    # 0 disables a rule: nothing is deleted
    r = run(env, "prune", "--max-age-days", "0", "--max-rows", "0")
    assert "Pruned 0 trace(s); 3 left." in r.output
    time.sleep(0.3)
    r = run(env, "prune", "--max-age-days", "0.000001", "--max-rows", "0")  # ~86 ms
    assert "Pruned 3 trace(s); 0 left." in r.output


def test_prune_defaults_use_the_settings(make_env):
    env = make_env(settings={"max_rows": 3, "max_age_days": 0})
    _fill(env, 5)
    r = run(env, "prune")
    assert r.exit_code == 0 and "Pruned 2 trace(s); 3 left." in r.output


def test_prune_rejects_negative_values(make_env):
    env = make_env()
    for args in (["--max-rows", "-1"], ["--max-age-days", "-2"]):
        r = run(env, "prune", *args)
        assert r.exit_code == 2 and "must not be negative" in r.output


def test_commands_work_with_the_factory_pattern(make_env):
    env = make_env(factory=True)
    _fill(env, 2)
    assert "Deleted 2 trace(s)." in run(env, "clear", "-y").output
