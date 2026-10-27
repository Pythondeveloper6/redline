from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from fastapi_redline import instrument_engine
from fastapi_redline._sqlalchemy import name_engines
from redline_core import Recorder, Settings


def test_no_trace_is_a_noop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    instrument_engine(engine)
    with engine.connect() as conn:
        assert conn.execute(text("select 1")).scalar() == 1


def test_executemany_and_rowcount_and_vendor(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    instrument_engine(engine, "main")
    rec = Recorder(Settings(project_root=str(tmp_path)))
    with rec.recording(method="GET", path="/x") as b:
        with engine.begin() as conn:
            conn.execute(text("create table t (id integer, name text)"))
            conn.execute(
                text("insert into t (id, name) values (:id, :name)"),
                [{"id": i, "name": f"n{i}"} for i in range(30)],
            )
            conn.execute(text("update t set name = 'z' where id < 5"))
            conn.execute(text("select * from t where id = :i"), {"i": 3}).all()
    trace = rec.last_trace
    by_kind = {s.data["sql"].split()[0].lower(): s for s in trace.spans if s.kind == "sql"}
    assert b.query_count == 4
    ins = by_kind["insert"]
    assert ins.data["many"] is True and ins.data["explainable"] is False
    assert len(ins.data["params"]) == 10  # first 10 rows kept
    assert by_kind["update"].data["rows"] == 5
    sel = by_kind["select"]
    assert sel.data["vendor"] == "sqlite" and sel.data["db_alias"] == "main"
    assert sel.data["explainable"] is True


def test_failed_statement_does_not_leak_state(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    instrument_engine(engine)
    rec = Recorder(Settings(project_root=str(tmp_path)))
    with rec.recording(method="GET", path="/x"):
        with engine.connect() as conn:
            try:
                conn.execute(text("select * from missing"))
            except Exception:
                pass
            conn.execute(text("select 1")).all()
    sql = [s.data["sql"] for s in rec.last_trace.spans if s.kind == "sql"]
    assert sql == ["select 1"]


def test_instrument_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    instrument_engine(engine)
    instrument_engine(engine)
    rec = Recorder(Settings(project_root=str(tmp_path)))
    with rec.recording(method="GET", path="/x"):
        with engine.connect() as conn:
            conn.execute(text("select 1")).all()
    assert len([s for s in rec.last_trace.spans if s.kind == "sql"]) == 1


def test_engine_aliases(tmp_path):
    a = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    b = create_engine(f"sqlite:///{tmp_path / 'analytics.db'}")
    c = create_engine("sqlite://")
    assert list(name_engines(a)) == ["default"]
    assert list(name_engines([a, b, c])) == ["default", "analytics", "db3"]
    assert list(name_engines({"x": a, "y": b})) == ["x", "y"]
    assert name_engines(None) == {}


def test_add_engine_after_install(make_env, tmp_path):
    env = make_env(engine_mode="sync")
    extra = create_engine(f"sqlite:///{tmp_path / 'extra.db'}")
    assert env.redline.add_engine(extra) == "extra"
    with TestClient(env.app) as c:
        assert c.get("/sync-count").status_code == 200
    assert env.trace().query_count == 1


def test_query_in_background_thread_is_not_attributed(make_env):
    # A plain Thread does not inherit the request context (documented behaviour).
    import threading

    env = make_env(engine_mode="sync")
    from redline_core.recorder import current

    seen = []
    with TestClient(env.app) as c:
        c.get("/sync-count")

    def work():
        seen.append(current())

    th = threading.Thread(target=work)
    th.start()
    th.join()
    assert seen == [None]


def _count(engine):
    with engine.connect() as conn:
        return conn.execute(text("select count(*) from tags")).scalar()


def test_sync_executor_always_rolls_back(make_env):
    from fastapi_redline._sqlalchemy import build_executor

    env = make_env(engine_mode="sync")
    before = _count(env.sync_engine)
    run = build_executor(env.sync_engine, "sqlite", lambda: None)
    # even a (hypothetical) write is rolled back; positional params arrive as a list
    run("insert into tags (name) values (?)", ["tmp"])
    assert _count(env.sync_engine) == before
    rows = run("EXPLAIN QUERY PLAN select * from tags where id = ?", [1])
    assert rows and "tags" in str(rows[0]).lower()


async def test_async_executor_always_rolls_back(make_env):
    import asyncio

    from fastapi_redline._sqlalchemy import build_executor

    env = make_env(engine_mode="async")
    before = _count(env.sync_engine)
    loop = asyncio.get_running_loop()
    run = build_executor(env.async_engine, "sqlite", lambda: loop)
    await asyncio.to_thread(run, "insert into tags (name) values (?)", ["tmp"])
    assert _count(env.sync_engine) == before
    rows = await asyncio.to_thread(run, "EXPLAIN QUERY PLAN select * from tags where id = ?", [1])
    assert rows
