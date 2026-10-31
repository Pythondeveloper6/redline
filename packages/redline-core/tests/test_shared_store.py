"""Shared-store selection, the PostgreSQL dialect layer and the doctor's deployment checks.

None of these needs a PostgreSQL server (``test_storage_postgres`` runs against one).
"""

from __future__ import annotations

import sys
from decimal import Decimal

import pytest

from redline_core import cli
from redline_core.conf import Settings, SettingsError
from redline_core.config import resolve_settings
from redline_core.storage import PostgresStore, SQLiteStore, store_from_settings
from redline_core.storage import postgres as pg
from redline_core.storage.safety import detect_workers

URL = "postgresql://shop:s3cret@db.internal:5432/redline"


# ── settings ────────────────────────────────────────────────────────────────────────────
def test_storage_url_defaults_to_none_and_must_be_postgres():
    assert Settings().storage_url is None
    assert Settings(storage_url=URL).storage_url == URL
    Settings(storage_url="postgres://h/db")  # the short scheme is accepted
    with pytest.raises(SettingsError, match="postgresql://"):
        Settings(storage_url="mysql://h/db")
    with pytest.raises(SettingsError):
        Settings(storage_url=5)  # type: ignore[arg-type]


def test_storage_url_from_the_environment_is_masked_in_public_config():
    resolved = resolve_settings(None, read_file=False, environ={"REDLINE_STORAGE_URL": URL})
    assert resolved.settings.storage_url == URL
    assert resolved.sources["storage_url"] == "env"
    public = resolved.public()
    assert "s3cret" not in str(public)
    assert public["settings"]["storage_url"] == "postgresql://shop:***@db.internal:5432/redline"
    assert "storage_url" in public["groups"]["storage"]


def test_mask_url():
    assert pg.mask_url(URL) == "postgresql://shop:***@db.internal:5432/redline"
    assert pg.mask_url("postgresql://shop@h/db") == "postgresql://shop@h/db"
    assert pg.mask_url("postgresql://h/db?user=a&password=pw&x=1") == (
        "postgresql://h/db?user=a&password=***&x=1"
    )
    assert pg.is_postgres_url(URL) and not pg.is_postgres_url("/var/lib/redline.db")


# ── selection ───────────────────────────────────────────────────────────────────────────
class _FakePostgres:
    calls: list = []

    @classmethod
    def from_settings(cls, settings, **kwargs):
        cls.calls.append((settings.storage_url, kwargs))
        return "shared-store"


def test_store_from_settings_defaults_to_sqlite(tmp_path):
    store = store_from_settings(
        Settings(storage_path=str(tmp_path / "r.db"), allow_storage_in_repo=True)
    )
    try:
        assert type(store) is SQLiteStore and store.path.endswith("r.db")
    finally:
        store.close()


def test_storage_url_selects_the_postgres_store(monkeypatch, tmp_path):
    import redline_core.storage as storage

    _FakePostgres.calls = []
    monkeypatch.setattr(storage, "PostgresStore", _FakePostgres)
    settings = Settings(storage_url=URL, storage_path=str(tmp_path / "unused.db"))
    assert store_from_settings(settings, background=False) == "shared-store"
    # code that builds the store with SQLiteStore.from_settings follows the setting too
    assert SQLiteStore.from_settings(settings) == "shared-store"
    assert _FakePostgres.calls == [(URL, {"background": False}), (URL, {})]
    assert not (tmp_path / "unused.db").exists()


@pytest.mark.parametrize(
    "module, attr",
    [
        ("fastapi_redline.integration", "store_from_settings"),
        ("flask_redline.integration", "store_from_settings"),
        ("litestar_redline.plugin", "store_from_settings"),
        ("django_redline.conf", "store_from_settings"),
    ],
)
def test_adapters_build_their_default_store_from_settings(module, attr):
    mod = pytest.importorskip(module)
    assert getattr(mod, attr) is store_from_settings


def test_postgres_store_needs_the_driver(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ImportError, match=r"redline-core\[postgres\]"):
        PostgresStore(URL)


def test_postgres_store_rejects_bad_arguments():
    with pytest.raises(ValueError, match="postgresql://"):
        PostgresStore("/tmp/redline.db")
    with pytest.raises(ValueError, match="schema"):
        PostgresStore(URL, schema="bad; DROP")


# ── dialect ─────────────────────────────────────────────────────────────────────────────
def test_translate_placeholders_and_percent_signs():
    sql = "SELECT 1 FROM t WHERE a LIKE ? ESCAPE '\\' AND b = '?' AND c LIKE '50%' AND d = ?"
    assert pg.translate(sql) == (
        "SELECT 1 FROM t WHERE a LIKE %s ESCAPE '\\' AND b = '?' AND c LIKE '50%%' AND d = %s"
    )
    assert pg.translate("SELECT id FROM traces LIMIT -1 OFFSET ?") == (
        "SELECT id FROM traces LIMIT ALL OFFSET %s"
    )


def test_translate_insert_or_replace_into_an_upsert():
    assert pg.translate("INSERT OR REPLACE INTO profiles VALUES (?, ?)") == (
        "INSERT INTO profiles  VALUES (%s, %s) ON CONFLICT (trace_id) DO UPDATE SET "
        "data = EXCLUDED.data"
    )
    meta = pg.translate("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_seq', ?)")
    assert meta.endswith("ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    steps = pg.translate(
        "INSERT OR REPLACE INTO run_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    assert "ON CONFLICT (run_id, step) DO UPDATE SET started_at = EXCLUDED.started_at" in steps
    traces = pg.translate("INSERT OR REPLACE INTO traces VALUES (?)")
    assert traces.endswith("saved_seq = EXCLUDED.saved_seq")  # added by a migration
    with pytest.raises(ValueError, match="primary key"):
        pg.translate("INSERT OR REPLACE INTO trace_tags VALUES (?, ?, ?)")


def test_every_table_maps_to_postgres_types():
    ddl = "\n".join(pg._ddl_statements())
    for sqlite_only in (" REAL", " BLOB", " INTEGER"):
        assert sqlite_only not in ddl
    assert "DOUBLE PRECISION" in ddl and "BYTEA" in ddl and 'TEXT COLLATE "C"' in ddl
    tables = pg._tables()
    assert tables["rollups"][1] == ["bucket", "release", "route", "method"]
    assert tables["traces"][0][-1] == "saved_seq" and len(tables["traces"][0]) == 17
    assert {"check_runs", "check_results", "issue_index", "lifecycle_hourly"} <= set(tables)


def test_row_behaves_like_sqlite_row():
    index = {"n": 0, "total": 1, "name": 2}
    row = pg.Row(index, (Decimal("3"), Decimal("1.5"), "x"))
    assert row["n"] == 3 and type(row["n"]) is int
    assert row[1] == 1.5 and type(row[1]) is float
    assert dict(row) == {"n": 3, "total": 1.5, "name": "x"} and list(row) == [3, 1.5, "x"]
    assert len(row) == 3 and row.keys() == ["n", "total", "name"]


def test_params_are_adapted():
    assert pg._params([True, "a\x00b", None, 1.5]) == [1, "a�b", None, 1.5]


# ── worker detection ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "environ, expected",
    [
        ({}, None),
        ({"WEB_CONCURRENCY": "4"}, (4, "WEB_CONCURRENCY=4")),
        ({"WEB_CONCURRENCY": "auto"}, None),
        ({"UVICORN_WORKERS": "3"}, (3, "UVICORN_WORKERS=3")),
        (
            {"GUNICORN_CMD_ARGS": "--bind 0.0.0.0:8000 --workers 6"},
            (6, "GUNICORN_CMD_ARGS --workers 6"),
        ),
        ({"GUNICORN_CMD_ARGS": "--workers=2 --timeout 30"}, (2, "GUNICORN_CMD_ARGS --workers 2")),
        ({"GUNICORN_CMD_ARGS": "-w 5"}, (5, "GUNICORN_CMD_ARGS --workers 5")),
        ({"GUNICORN_CMD_ARGS": "-w8"}, (8, "GUNICORN_CMD_ARGS --workers 8")),
        ({"GUNICORN_CMD_ARGS": "--threads 4"}, None),
        ({"GUNICORN_CMD_ARGS": "--workers 2", "WEB_CONCURRENCY": "9"}, (9, "WEB_CONCURRENCY=9")),
        ({"GUNICORN_CMD_ARGS": "'unbalanced"}, None),
    ],
)
def test_detect_workers(environ, expected):
    assert detect_workers(environ) == expected


# ── doctor ──────────────────────────────────────────────────────────────────────────────
def _resolved(**values):
    return resolve_settings(values, read_file=False, environ={})


def _storage_checks(checks):
    return [c for c in checks if c[1] in ("storage", "filesystem", "workers", "storage WAL")]


def test_doctor_postgres_store_without_driver(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "psycopg" else real(name, *a)
    )
    checks = _storage_checks(cli._doctor_checks(_resolved(storage_url=URL), environ={}))
    assert checks == [
        (
            "fail",
            "storage",
            'storage_url needs the PostgreSQL driver: pip install "redline-core[postgres]"',
        )
    ]


def test_doctor_postgres_store_unreachable_and_reachable(monkeypatch):
    def refused(url, timeout=5.0):
        raise OSError("connection refused")

    monkeypatch.setattr(pg, "ping", refused)
    checks = _storage_checks(cli._doctor_checks(_resolved(storage_url=URL), environ={}))
    assert checks[0][0] == "fail" and "OSError" in checks[0][2]
    assert "s3cret" not in checks[0][2] and "shop:***@db.internal" in checks[0][2]

    monkeypatch.setattr(pg, "ping", lambda url, timeout=5.0: "17.2")
    checks = cli._doctor_checks(
        _resolved(storage_url=URL), environ={"WEB_CONCURRENCY": "8"}, strict=True
    )
    storage = _storage_checks(checks)
    # a shared store is what several workers should use: no worker warning, no file probes
    assert storage == [
        ("ok", "storage", f"PostgreSQL 17.2 at {pg.mask_url(URL)}, shared by every worker")
    ]


def test_doctor_warns_when_workers_share_a_sqlite_file(tmp_path):
    resolved = _resolved(storage_path=str(tmp_path / "r.db"))
    assert not [c for c in cli._doctor_checks(resolved, environ={}) if c[1] == "workers"]
    single = cli._doctor_checks(resolved, environ={"WEB_CONCURRENCY": "1"})
    assert not [c for c in single if c[1] == "workers"]
    warn = [
        c
        for c in cli._doctor_checks(resolved, environ={"WEB_CONCURRENCY": "4"})
        if c[1] == "workers"
    ]
    assert warn and warn[0][0] == "warn"
    assert "4 worker processes (WEB_CONCURRENCY=4)" in warn[0][2] and "storage_url" in warn[0][2]
    strict = [
        c
        for c in cli._doctor_checks(resolved, environ={}, workers=3, strict=True)
        if c[1] == "workers"
    ]
    assert strict == [(strict[0][0], "workers", strict[0][2])] and strict[0][0] == "fail"
    assert "(--workers)" in strict[0][2]
    memory = _resolved(storage_path=":memory:")
    assert not [c for c in cli._doctor_checks(memory, workers=4, strict=True) if c[1] == "workers"]


def test_doctor_network_filesystem_fails_when_strict(tmp_path, monkeypatch):
    import redline_core.storage.safety as safety

    monkeypatch.setattr(safety, "filesystem_type", lambda path: "nfs4")
    resolved = _resolved(storage_path=str(tmp_path / "r.db"))
    loose = [c for c in cli._doctor_checks(resolved, environ={}) if c[1] == "filesystem"]
    strict = [
        c for c in cli._doctor_checks(resolved, environ={}, strict=True) if c[1] == "filesystem"
    ]
    assert loose[0][0] == "warn" and strict[0][0] == "fail"
    assert "nfs4" in strict[0][2] and "PostgreSQL" in strict[0][2]


@pytest.fixture
def project(tmp_path, monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("REDLINE_") and not key.startswith("REDLINE_TEST_"):
            monkeypatch.delenv(key)
    for key in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_CMD_ARGS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "redline.toml").write_text('storage_path = "recordings.db"\n')
    return tmp_path


def test_doctor_cli_strict_and_workers_flags(project, monkeypatch):
    from .test_cli_config import run

    code, out, _ = run("doctor", "--workers", "4")
    assert "WARN  workers" in out and "FAIL  workers" not in out
    code, out, _ = run("doctor", "--workers", "4", "--strict")
    assert code == 1 and "FAIL  workers" in out
    monkeypatch.setenv("GUNICORN_CMD_ARGS", "--workers 3")
    code, out, _ = run("doctor", "--strict", "--json")
    assert code == 1 and "GUNICORN_CMD_ARGS --workers 3" in out
