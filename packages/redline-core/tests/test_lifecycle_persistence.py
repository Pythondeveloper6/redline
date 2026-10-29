from __future__ import annotations

from redline_core import Settings
from redline_core.recorder import Recorder
from redline_core.storage.sqlite import SQLiteStore


class _Store:
    def __init__(self):
        self.calls = []

    def record_lifecycle(self, metrics, *, release=None):
        self.calls.append((metrics, release))


def test_connection_health_snapshot_names_the_pool(monkeypatch):
    from redline_core import connection_health

    class Pool:
        pass

    monkeypatch.setattr(connection_health, "_pid", connection_health.os.getpid())
    pool = Pool()
    connection_health.register(pool, alias="primary", vendor="mssql")
    connection_health.connected(pool)
    connection_health.connected(pool)
    snapshot = connection_health.snapshot()
    entry = next(item for item in snapshot if item["alias"] == "primary")
    assert entry["vendor"] == "mssql"
    assert entry["connects_hour"] >= 2
    assert entry["fresh_failures_hour"] == 0


def test_store_keeps_the_highest_hourly_gauge(tmp_path):
    store = SQLiteStore(str(tmp_path / "redline.db"), background=False)
    try:
        store.record_lifecycle(
            [{"alias": "primary", "vendor": "mssql", "connects_hour": 2, "fresh_failures_hour": 1}],
            release="0.4",
        )
        store.record_lifecycle(
            [{"alias": "primary", "vendor": "mssql", "connects_hour": 5, "fresh_failures_hour": 0}],
            release="0.4",
        )
        rows = store.lifecycle(3600)
        entry = next(row for row in rows if row["db_alias"] == "primary")
        assert entry["connects_hour"] == 5
        assert entry["fresh_failures_hour"] == 1
        assert entry["release"] == "0.4"
    finally:
        store.close()


def test_store_skips_an_unchanged_gauge_within_a_minute(tmp_path):
    store = SQLiteStore(str(tmp_path / "redline.db"), background=False)
    try:
        metrics = [
            {"alias": "primary", "vendor": "sqlite", "connects_hour": 1, "fresh_failures_hour": 0}
        ]
        store.record_lifecycle(metrics, release="0.4")
        store.record_lifecycle(metrics, release="0.4")
        rows = store.lifecycle(3600)
        assert len([row for row in rows if row["db_alias"] == "primary"]) == 1
    finally:
        store.close()


def test_recorder_flushes_lifecycle_metrics_even_without_a_trace(monkeypatch):
    from redline_core import connection_health

    monkeypatch.setattr(
        connection_health,
        "snapshot",
        lambda: [
            {"alias": "primary", "vendor": "mssql", "connects_hour": 3, "fresh_failures_hour": 0}
        ],
    )
    store = _Store()
    recorder = Recorder(Settings(release="0.4"), store)
    recorder._persist_lifecycle()
    assert store.calls == [
        (
            [
                {
                    "alias": "primary",
                    "vendor": "mssql",
                    "connects_hour": 3,
                    "fresh_failures_hour": 0,
                }
            ],
            "0.4",
        )
    ]
