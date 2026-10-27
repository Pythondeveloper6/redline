from __future__ import annotations

import pytest
from flask import Flask
from sqlalchemy import create_engine, event, text

from flask_redline import Redline, instrument_engine
from flask_redline._sqlalchemy import build_executor, name_engines
from redline_core import Recorder, Settings

from . import fsa_app
from .conftest import ROOT, BufferedClient
from .fsa_app import db

# ── plain SQLAlchemy ─────────────────────────────────────────────────────────────────


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


def test_engine_aliases(tmp_path):
    a = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    b = create_engine(f"sqlite:///{tmp_path / 'analytics.db'}")
    c = create_engine("sqlite://")
    assert list(name_engines(a)) == ["default"]
    assert list(name_engines([a, b, c])) == ["default", "analytics", "db3"]
    assert list(name_engines({"x": a, "y": b})) == ["x", "y"]
    assert name_engines(None) == {}


def test_engine_list_and_mapping(make_env, tmp_path):
    other = create_engine(f"sqlite:///{tmp_path / 'analytics.db'}")
    env = make_env(engine=None)
    env.redline.add_engine(env.engine, "main")
    assert set(env.redline.engines) == {"main"}
    assert env.redline.add_engine(other) == "analytics"
    env.client().get("/dup")
    assert {s.data["db_alias"] for s in env.trace().spans if s.kind == "sql"} == {"main"}


def test_explicit_engine_list(tmp_path):
    a = create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    b = create_engine(f"sqlite:///{tmp_path / 'b.db'}")
    app = Flask(__name__)
    rl = Redline(
        app,
        engine=[a, b],
        settings={"storage_path": str(tmp_path / "r.db")},
        permission=lambda request: True,
    )
    try:
        assert list(rl.engines) == ["default", "b"]
    finally:
        rl.close()


def test_add_engine_after_install(make_env, tmp_path):
    env = make_env()
    extra = create_engine(f"sqlite:///{tmp_path / 'extra.db'}")
    assert env.redline.add_engine(extra) == "extra"
    assert set(env.redline.engines) == {"default", "extra"}


def test_executor_always_rolls_back(make_env):
    env = make_env()

    def count():
        with env.engine.connect() as conn:
            return conn.execute(text("select count(*) from tags")).scalar()

    before = count()
    run = build_executor(env.engine, "sqlite", lambda: None)
    # even a (hypothetical) write is rolled back; positional params arrive as a list
    run("insert into tags (name) values (?)", ["tmp"])
    assert count() == before
    rows = run("EXPLAIN QUERY PLAN select * from tags where id = ?", [1])
    assert rows and "tags" in str(rows[0]).lower()


def test_query_in_background_thread_is_not_attributed(make_env):
    # A plain Thread does not inherit the request context (documented behaviour).
    import threading

    from redline_core.recorder import current

    env = make_env()
    seen = []
    env.client().get("/dup")

    def work():
        seen.append(current())

    th = threading.Thread(target=work)
    th.start()
    th.join()
    assert seen == [None]


def test_disabled_installs_no_listeners(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'd.db'}")
    app = Flask(__name__)
    rl = Redline(
        app,
        engine=engine,
        settings={"enabled": False, "storage_path": str(tmp_path / "r.db")},
        permission=lambda request: True,
    )
    try:
        assert not list(engine.dispatch.before_cursor_execute)
        assert "default" in rl.engines  # EXPLAIN still knows the engine
        assert not event.contains(engine, "after_cursor_execute", lambda *a: None)
    finally:
        rl.close()


# ── Flask-SQLAlchemy ─────────────────────────────────────────────────────────────────


def _fsa(tmp_path, *, before_db: bool = True, **kw):
    uri = f"sqlite:///{tmp_path / 'main.db'}"
    audit = f"sqlite:///{tmp_path / 'audit.db'}"
    app = fsa_app.create_app(uri, audit)
    app.test_client_class = BufferedClient
    cfg = {
        "storage_path": str(tmp_path / "redline.db"),
        "project_root": str(ROOT),
        "access": "local",  # set explicitly: unset answers 404 outside debug mode
    }
    redline = Redline(settings=cfg, **kw)
    if before_db:  # Redline first: the engines do not exist yet
        redline.init_app(app)
    db.init_app(app)
    if not before_db:
        redline.init_app(app)
    with app.app_context():
        db.create_all()
        fsa_app.seed()
    return app, redline


@pytest.mark.parametrize("before_db", [True, False])
def test_flask_sqlalchemy_engines_are_auto_instrumented(tmp_path, before_db):
    app, redline = _fsa(tmp_path, before_db=before_db)
    try:
        c = app.test_client()
        assert c.get("/authors").status_code == 200
        redline.store.flush(5)
        t = redline.store.get(redline.store.list(None, limit=5).items[0]["id"])
        assert t.route == "/authors" and t.query_count == 1 + 8
        assert {s.data["db_alias"] for s in t.spans if s.kind == "sql"} == {"default"}
        issue = next(i for i in t.issues if i.kind == "n_plus_one")
        assert issue.suggestion.code == "select(Author).options(selectinload(Author.books))"
        assert issue.suggestion.diff and "tests/fsa_app.py" in issue.suggestion.diff
        assert sorted(redline.engines) == ["audit", "default"]
    finally:
        redline.close()
        with app.app_context():
            db.engine.dispose()


def test_flask_sqlalchemy_many_to_one_joinedload(tmp_path):
    app, redline = _fsa(tmp_path)
    try:
        app.test_client().get("/books")
        redline.store.flush(5)
        t = redline.store.get(redline.store.list(None, limit=5).items[0]["id"])
        issue = next(i for i in t.issues if i.kind == "n_plus_one")
        assert issue.suggestion.code == "select(Book).options(joinedload(Book.author))"
    finally:
        redline.close()
        with app.app_context():
            db.engine.dispose()


def test_flask_sqlalchemy_binds_get_aliases_and_explain(tmp_path):
    app, redline = _fsa(tmp_path)
    try:
        c = app.test_client()
        c.get("/audit")
        redline.store.flush(5)
        assert set(redline.engines) == {"default", "audit"}
        t = redline.store.get(redline.store.list(None, limit=5).items[0]["id"])
        aliases = {s.data["db_alias"] for s in t.spans if s.kind == "sql"}
        assert "audit" in aliases
        sid = next(
            s.id
            for s in t.spans
            if s.kind == "sql" and s.data["db_alias"] == "audit" and s.data["explainable"]
        )
        r = c.post("/_redline/api/explain", json={"trace_id": t.id, "span_id": sid})
        assert r.status_code == 200, r.text
        assert "log" in r.json["text"].lower()
    finally:
        redline.close()
        with app.app_context():
            for engine in db.engines.values():
                engine.dispose()


def test_explicit_engine_wins_over_flask_sqlalchemy(tmp_path):
    own = create_engine(f"sqlite:///{tmp_path / 'own.db'}")
    app, redline = _fsa(tmp_path, engine=own)
    try:
        app.test_client().get("/authors")
        assert list(redline.engines) == ["default"] and redline.engines["default"] is own
        redline.store.flush(5)
        assert redline.store.count() == 1
        t = redline.store.get(redline.store.list(None, limit=5).items[0]["id"])
        assert t.query_count == 0  # Flask-SQLAlchemy's engine was not instrumented
    finally:
        redline.close()
        with app.app_context():
            db.engine.dispose()
        own.dispose()
