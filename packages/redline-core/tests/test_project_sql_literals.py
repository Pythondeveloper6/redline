from redline_core.check_sql import scan


def fixture(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (app / "queries.py").write_text('SQL = "SELECT 1 -- :hidden"\n')
    return app


def test_imported_literals_resolve_without_executing_project(tmp_path):
    app = fixture(tmp_path)
    (app / "view.py").write_text(
        'from .queries import SQL as query\nraise RuntimeError("must not run")\n'
        "def run(conn):\n    conn.execute(query)\n"
    )
    report = scan([str(app)])
    assert report["files"] == 3
    assert report["statements"] == 1
    assert not report["skipped"]
    assert report["findings"][0]["kind"] == "bind-in-comment"


def test_module_alias_constants_and_relative_root(tmp_path, monkeypatch):
    app = fixture(tmp_path)
    (app / "view.py").write_text(
        "import app.queries as q\ndef run(conn):\n    conn.execute(q.SQL)\n"
    )
    monkeypatch.chdir(tmp_path)
    report = scan(["app"])
    assert report["files"] == 3
    assert report["findings"] and not report["skipped"]


def test_imported_constants_in_package_initializer(tmp_path):
    app = fixture(tmp_path)
    (app / "__init__.py").write_text("from .queries import SQL\nconn.execute(SQL)\n")
    report = scan([str(app)])
    assert report["statements"] == 1
    assert not report["skipped"]


def test_shadowed_or_rebound_import_is_unresolved(tmp_path):
    app = fixture(tmp_path)
    (app / "view.py").write_text(
        "import app.queries as q\n"
        "def run(conn, q):\n    conn.execute(q.SQL)\n"
        "from .queries import SQL\nSQL = unknown()\nconn.execute(SQL)\n"
    )
    report = scan([str(app)])
    assert not report["statements"]
    assert len(report["skipped"]) == 2


def test_unscanned_or_symlinked_modules_are_not_read(tmp_path):
    app = fixture(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text('SQL = "SELECT 1 -- :hidden"\n')
    (app / "link.py").symlink_to(outside)
    (app / "view.py").write_text("from app.link import SQL\nconn.execute(SQL)\n")
    report = scan([str(app)])
    assert report["files"] == 3
    assert not report["statements"]
    assert report["skipped"]


def test_import_cycle_terminates_with_unresolved_inputs(tmp_path):
    app = fixture(tmp_path)
    (app / "a.py").write_text("from .b import SQL\nconn.execute(SQL)\n")
    (app / "b.py").write_text("from .a import SQL\nconn.execute(SQL)\n")
    report = scan([str(app)])
    assert report["files"] == 4
    assert len(report["skipped"]) == 2
