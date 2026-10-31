"""Refactor-check tables of the SQLite store (schema v4, ``CONTRACT.md`` §19.5).

``check_suites`` hold suites created in the UI, ``check_runs`` one row per baseline or
compare run (CLI, UI or an imported baseline) and ``check_results`` one row per request of a
run: its baseline data (``kind="baseline"``) or its comparison result (``kind="compare"``).
Writes are synchronous (``BEGIN IMMEDIATE``, like load runs) – they are rare and the
runner needs them visible at once.

Mixed into :class:`~redline_core.storage.sqlite.SQLStore` (so both the SQLite and the
PostgreSQL store); it uses the store's ``_query``, ``_write_now``, ``_lock``, ``_conn``
and ``path``.
"""

from __future__ import annotations

import builtins
import json
import os
import sqlite3
import time
import uuid
from typing import Any

__all__ = [
    "CHECK_RETENTION_PER_SUITE",
    "CHECK_RUN_KINDS",
    "CHECK_RUN_STATUSES",
    "DEAD_CHECK_MESSAGE",
    "SCHEMA_V4",
    "CheckStoreMixin",
]

#: v4: refactor checks. Additive; created by the migration (and for fresh files).
SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS check_suites (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    suite_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS check_runs (
    id TEXT PRIMARY KEY,
    suite_id TEXT,
    kind TEXT NOT NULL,
    baseline_id TEXT,
    status TEXT NOT NULL,
    label TEXT,
    git_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL,
    summary_json TEXT,
    error TEXT,
    suite_json TEXT NOT NULL,
    info_json TEXT NOT NULL,
    progress_json TEXT
);
CREATE INDEX IF NOT EXISTS check_runs_suite ON check_runs (suite_id, created_at DESC);
CREATE INDEX IF NOT EXISTS check_runs_status ON check_runs (status);
CREATE TABLE IF NOT EXISTS check_results (
    run_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    name TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (run_id, idx)
)
"""

CHECK_RUN_KINDS = ("baseline", "compare")
CHECK_RUN_STATUSES = ("running", "done", "failed", "stopped")
#: Runs kept per suite (``NULL`` suite = one group); referenced baselines survive.
CHECK_RETENTION_PER_SUITE = 50
#: ``X-Redline-Check`` tags are kept for check runs that ended at most this long ago.
CHECK_TAG_GRACE_S = 60.0
DEAD_CHECK_MESSAGE = "the check process exited unexpectedly"
#: Keys of a run row (``check_runs`` columns as the API shows them).
_INFO_KEYS = ("base_url", "release", "baseline", "source")


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _loads(text: Any, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _new_id() -> str:
    return uuid.uuid4().hex


def _suite_requests(suite: Any) -> int:
    requests = suite.get("requests") if isinstance(suite, dict) else None
    return len(requests) if isinstance(requests, list) else 0


class CheckStoreMixin:
    """Store methods for suites, check runs and their per-request results."""

    path: str
    _db_errors: tuple[type[BaseException], ...] = (sqlite3.Error,)
    _check_cache: tuple[float, set[str]] | None = None
    #: The writer validates ``X-Redline-Check`` tags against :meth:`known_check_runs`, so the
    #: recorder may keep a sampled-out check call for it (``sampled_by="check"``).
    validates_check_tags = True
    #: A ``running`` check run younger than this is never judged dead.
    CHECK_GRACE_S = 10.0

    # helpers provided by SQLStore
    def _query(self, sql: str, params: Any = ()) -> builtins.list[sqlite3.Row]:  # pragma: no cover
        raise NotImplementedError

    def _write_now(self, statements: builtins.list[tuple[str, Any]]) -> None:  # pragma: no cover
        raise NotImplementedError

    # suites ------------------------------------------------------------------
    def create_check_suite(
        self, name: str, suite: dict[str, Any], suite_id: str | None = None
    ) -> dict[str, Any]:
        """Store a new suite; returns its record (``id``, ``name``, ``suite``, times)."""
        now = time.time()
        sid = suite_id or _new_id()
        self._write_now(
            [
                (
                    "INSERT INTO check_suites VALUES (?, ?, ?, ?, ?)",
                    (sid, name, _dumps(suite), now, now),
                )
            ]
        )
        return {"id": sid, "name": name, "suite": suite, "created_at": now, "updated_at": now}

    def update_check_suite(
        self, suite_id: str, name: str, suite: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Replace a suite's name and definition; ``None`` for an unknown id."""
        now = time.time()
        with self._lock:  # type: ignore[attr-defined]
            c = self._conn  # type: ignore[attr-defined]
            c.execute("BEGIN IMMEDIATE")
            try:
                n = c.execute(
                    "UPDATE check_suites SET name = ?, suite_json = ?, updated_at = ? WHERE id = ?",
                    (name, _dumps(suite), now, suite_id),
                ).rowcount
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return self.get_check_suite(suite_id) if n else None

    def get_check_suite(self, suite_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM check_suites WHERE id = ?", (suite_id,))
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"],
            "name": r["name"],
            "suite": _loads(r["suite_json"], {}),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }

    def list_check_suites(self) -> builtins.list[dict[str, Any]]:
        """Suites, most recently updated first, each with ``requests`` (count),
        ``last_baseline`` and ``last_compare`` (brief run dicts or ``None``)."""
        self._reap_dead_check_runs()
        suites = self._query("SELECT * FROM check_suites ORDER BY updated_at DESC, id")
        out = []
        for r in suites:
            suite = _loads(r["suite_json"], {})
            last: dict[str, Any] = {}
            for kind in CHECK_RUN_KINDS:
                rows = self._query(
                    "SELECT * FROM check_runs WHERE suite_id = ? AND kind = ? "
                    "ORDER BY created_at DESC, id LIMIT 1",
                    (r["id"], kind),
                )
                last[kind] = _brief(rows[0]) if rows else None
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "requests": _suite_requests(suite),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "last_baseline": last["baseline"],
                    "last_compare": last["compare"],
                }
            )
        return out

    def delete_check_suite(self, suite_id: str) -> int:
        """Delete a suite with its runs and their results; returns 1 or 0."""
        with self._lock:  # type: ignore[attr-defined]
            c = self._conn  # type: ignore[attr-defined]
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute(
                    "DELETE FROM check_results WHERE run_id IN "
                    "(SELECT id FROM check_runs WHERE suite_id = ?)",
                    (suite_id,),
                )
                c.execute("DELETE FROM check_runs WHERE suite_id = ?", (suite_id,))
                n = c.execute("DELETE FROM check_suites WHERE id = ?", (suite_id,)).rowcount
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return n

    # runs ----------------------------------------------------------------------
    def create_check_run(self, run: dict[str, Any]) -> str:
        """Insert or update a check run row (``{id, suite_id, kind, baseline_id, label, git,
        created_at, suite, status?, base_url?, release?, baseline?, source?}``).

        Upserts: a row pre-inserted by the UI launcher keeps its ``created_at``; a second
        ``start`` from the engine fills in what it knows. Applies retention to the suite.
        """
        rid = str(run.get("id") or _new_id())
        kind = run.get("kind") or "compare"
        if kind not in CHECK_RUN_KINDS:
            raise ValueError(f"kind must be one of {', '.join(CHECK_RUN_KINDS)}")
        status = run.get("status") or "running"
        if status not in CHECK_RUN_STATUSES:
            raise ValueError(f"status must be one of {', '.join(CHECK_RUN_STATUSES)}")
        info = {k: run[k] for k in _INFO_KEYS if run.get(k) is not None}
        old = self._query("SELECT info_json FROM check_runs WHERE id = ?", (rid,))
        if old:
            info = {**(_loads(old[0]["info_json"], {}) or {}), **info}
        if kind == "compare" and run.get("baseline_id") and "baseline" not in info:
            brief = self._baseline_brief(str(run["baseline_id"]))
            if brief is not None:
                info["baseline"] = brief
        created = float(run.get("created_at") or time.time())
        self._write_now(
            [
                (
                    "INSERT INTO check_runs (id, suite_id, kind, baseline_id, status, label, "
                    "git_json, created_at, finished_at, summary_json, error, suite_json, "
                    "info_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "suite_id = COALESCE(excluded.suite_id, check_runs.suite_id), "
                    "kind = excluded.kind, "
                    "baseline_id = COALESCE(excluded.baseline_id, check_runs.baseline_id), "
                    "label = COALESCE(excluded.label, check_runs.label), "
                    "git_json = CASE WHEN excluded.git_json = 'null' THEN check_runs.git_json "
                    "ELSE excluded.git_json END, "
                    "suite_json = CASE WHEN excluded.suite_json = 'null' "
                    "THEN check_runs.suite_json ELSE excluded.suite_json END, "
                    "info_json = excluded.info_json",
                    (
                        rid,
                        run.get("suite_id"),
                        kind,
                        run.get("baseline_id"),
                        status,
                        run.get("label"),
                        _dumps(run.get("git")),
                        created,
                        run.get("finished_at"),
                        _dumps(run["summary"]) if run.get("summary") is not None else None,
                        run.get("error"),
                        _dumps(run.get("suite")),
                        _dumps(info),
                    ),
                )
            ]
        )
        self._check_cache = None
        self.prune_check_runs(run.get("suite_id"))
        return rid

    def _baseline_brief(self, baseline_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT id, label, git_json, created_at FROM check_runs WHERE id = ?", (baseline_id,)
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"],
            "label": r["label"],
            "git": _loads(r["git_json"]),
            "created_at": r["created_at"],
        }

    def set_check_progress(self, run_id: str, progress: dict[str, Any]) -> None:
        """Live progress of a running check (``{done, total, ...}``)."""
        self._write_now(
            [
                (
                    "UPDATE check_runs SET progress_json = ? WHERE id = ? AND status = 'running'",
                    (_dumps(progress), run_id),
                )
            ]
        )

    def finish_check_run(
        self,
        run_id: str,
        status: str,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Mark a check run ``done``/``failed``/``stopped`` with its summary or error."""
        if status not in CHECK_RUN_STATUSES or status == "running":
            raise ValueError("status must be done, failed or stopped")
        self._write_now(
            [
                (
                    "UPDATE check_runs SET status = ?, finished_at = ?, "
                    "summary_json = COALESCE(?, summary_json), error = ? WHERE id = ?",
                    (
                        status,
                        time.time(),
                        _dumps(summary) if summary is not None else None,
                        error,
                        run_id,
                    ),
                )
            ]
        )
        self._check_cache = None

    def add_check_result(self, run_id: str, idx: int, name: str, data: dict[str, Any]) -> None:
        """Insert or replace the result of request ``idx`` of a run."""
        self._write_now(
            [
                (
                    "INSERT OR REPLACE INTO check_results VALUES (?, ?, ?, ?)",
                    (run_id, int(idx), str(name), _dumps(data)),
                )
            ]
        )

    def get_check_results(self, run_id: str) -> builtins.list[dict[str, Any]]:
        """``[{idx, name, data}]`` in request order."""
        rows = self._query(
            "SELECT idx, name, data_json FROM check_results WHERE run_id = ? ORDER BY idx",
            (run_id,),
        )
        return [
            {"idx": r["idx"], "name": r["name"], "data": _loads(r["data_json"], {})} for r in rows
        ]

    def get_check_run(self, run_id: str) -> dict[str, Any] | None:
        """The run row as a dict (without results), or ``None``."""
        self._reap_dead_check_runs()
        rows = self._query("SELECT * FROM check_runs WHERE id = ?", (run_id,))
        return _run_dict(rows[0], full=True) if rows else None

    def list_check_runs(
        self, suite_id: str | None = None, limit: int = 50, kind: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Most recent first; ``suite_id``/``kind`` filter (no suite in the rows)."""
        self._reap_dead_check_runs()
        where, params = [], []
        if suite_id is not None:
            where.append("suite_id = ?")
            params.append(suite_id)
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        sql = "SELECT * FROM check_runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id LIMIT ?"
        params.append(max(1, int(limit)))
        return [_run_dict(r) for r in self._query(sql, params)]

    def delete_check_run(self, run_id: str) -> int:
        """Delete a check run and its results (tagged traces stay); returns 1 or 0."""
        with self._lock:  # type: ignore[attr-defined]
            c = self._conn  # type: ignore[attr-defined]
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("DELETE FROM check_results WHERE run_id = ?", (run_id,))
                n = c.execute("DELETE FROM check_runs WHERE id = ?", (run_id,)).rowcount
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return n

    def check_baseline(self, run_id: str) -> dict[str, Any] | None:
        """A stored baseline run as a baseline document (``format: redline-check-baseline``),
        ``None`` when ``run_id`` is not a finished baseline."""
        run = self.get_check_run(run_id)
        if run is None or run["kind"] != "baseline" or run["status"] != "done":
            return None
        return {
            "format": "redline-check-baseline",
            "version": 1,
            "id": run["id"],
            "suite": run["suite"] if isinstance(run["suite"], dict) else {},
            "label": run["label"],
            "git": run["git"],
            "release": run.get("release"),
            "base_url": run.get("base_url"),
            "status": run["status"],
            "created_at": run["created_at"],
            "requests": [r["data"] for r in self.get_check_results(run_id)],
        }

    def import_check_baseline(self, doc: dict[str, Any], suite_id: str | None = None) -> str:
        """Store a baseline document (already validated and redacted) as a ``done``
        baseline run; returns its new id."""
        rid = _new_id()
        created = doc.get("created_at")
        created = float(created) if isinstance(created, (int, float)) else time.time()
        statements: builtins.list[tuple[str, Any]] = [
            (
                "INSERT INTO check_runs (id, suite_id, kind, baseline_id, status, label, git_json, "
                "created_at, finished_at, summary_json, error, suite_json, info_json) "
                "VALUES (?, ?, 'baseline', NULL, 'done', ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (
                    rid,
                    suite_id,
                    doc.get("label"),
                    _dumps(doc.get("git")),
                    created,
                    time.time(),
                    _dumps(doc.get("suite")),
                    _dumps(
                        {
                            k: v
                            for k, v in {"release": doc.get("release"), "source": "import"}.items()
                            if v is not None
                        }
                    ),
                ),
            )
        ]
        for idx, req in enumerate(doc.get("requests") or []):
            name = req.get("name") if isinstance(req, dict) else None
            statements.append(
                (
                    "INSERT INTO check_results VALUES (?, ?, ?, ?)",
                    (rid, idx, str(name or f"request {idx}"), _dumps(req)),
                )
            )
        self._write_now(statements)
        self.prune_check_runs(suite_id)
        return rid

    # tags, retention, reaping -----------------------------------------------------
    def known_check_runs(self) -> set[str]:
        """Ids of check runs that are running or ended less than a minute ago (re-read at
        most once a second; the writer thread's ``X-Redline-Check`` rule)."""
        now = time.monotonic()
        cached = self._check_cache
        if cached is not None and now - cached[0] < 1.0:
            return cached[1]
        try:
            rows = self._query(
                "SELECT id FROM check_runs WHERE status = 'running' OR finished_at >= ?",
                (time.time() - CHECK_TAG_GRACE_S,),
            )
            ids = {r["id"] for r in rows}
        except self._db_errors:  # pragma: no cover - e.g. a store being closed
            ids = set()
        self._check_cache = (now, ids)
        return ids

    def prune_check_runs(self, suite_id: str | None = None, keep: int | None = None) -> int:
        """Keep the ``keep`` (default :data:`CHECK_RETENTION_PER_SUITE`) most recent runs of
        ``suite_id`` (``None``: runs without a suite), plus running ones and baselines a
        kept compare run refers to."""
        keep = CHECK_RETENTION_PER_SUITE if keep is None else keep
        if suite_id is None:
            rows = self._query(
                "SELECT id, kind, baseline_id, status FROM check_runs WHERE suite_id IS NULL "
                "ORDER BY created_at DESC, id"
            )
        else:
            rows = self._query(
                "SELECT id, kind, baseline_id, status FROM check_runs WHERE suite_id = ? "
                "ORDER BY created_at DESC, id",
                (suite_id,),
            )
        if len(rows) <= keep:
            return 0
        kept = rows[:keep]
        referenced = {r["baseline_id"] for r in kept if r["kind"] == "compare" and r["baseline_id"]}
        doomed = [
            r["id"] for r in rows[keep:] if r["status"] != "running" and r["id"] not in referenced
        ]
        if not doomed:
            return 0
        params = [(i,) for i in doomed]
        with self._lock:  # type: ignore[attr-defined]
            c = self._conn  # type: ignore[attr-defined]
            c.execute("BEGIN IMMEDIATE")
            try:
                c.executemany("DELETE FROM check_results WHERE run_id = ?", params)
                c.executemany("DELETE FROM check_runs WHERE id = ?", params)
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return len(doomed)

    def _reap_dead_check_runs(self) -> None:
        """Mark ``running`` check runs whose background process is gone as ``failed``
        (only where a ``runs`` directory sits beside the store file, like load runs)."""
        path = self.path
        if path == ":memory:" or path.startswith("file:"):
            return
        if not os.path.isdir(os.path.join(os.path.dirname(os.path.abspath(path)), "runs")):
            return
        rows = self._query(
            "SELECT id FROM check_runs WHERE status = 'running' AND created_at < ?",
            (time.time() - self.CHECK_GRACE_S,),
        )
        if not rows:
            return
        from ..load.control import run_process_alive

        for r in rows:
            try:
                alive = run_process_alive(r["id"], path)
            except ValueError:
                alive = False
            if not alive:
                self._write_now(
                    [
                        (
                            "UPDATE check_runs SET status = 'failed', finished_at = ?, error = ? "
                            "WHERE id = ? AND status = 'running'",
                            (time.time(), DEAD_CHECK_MESSAGE, r["id"]),
                        )
                    ]
                )
                self._check_cache = None


def _brief(r: sqlite3.Row) -> dict[str, Any]:
    """``last_baseline`` / ``last_compare`` of a suite list item."""
    return {
        "id": r["id"],
        "status": r["status"],
        "label": r["label"],
        "git": _loads(r["git_json"]),
        "created_at": r["created_at"],
        "summary": _loads(r["summary_json"]),
    }


def _run_dict(r: sqlite3.Row, *, full: bool = False) -> dict[str, Any]:
    info = _loads(r["info_json"], {}) or {}
    suite = _loads(r["suite_json"])
    out: dict[str, Any] = {
        "id": r["id"],
        "suite_id": r["suite_id"],
        "kind": r["kind"],
        "baseline_id": r["baseline_id"],
        "status": r["status"],
        "label": r["label"],
        "git": _loads(r["git_json"]),
        "created_at": r["created_at"],
        "finished_at": r["finished_at"],
        "summary": _loads(r["summary_json"]),
        "error": r["error"],
        "progress": _loads(r["progress_json"]),
        "name": suite.get("name") if isinstance(suite, dict) else None,
        "request_count": _suite_requests(suite),
        "base_url": info.get("base_url"),
        "release": info.get("release"),
        "source": info.get("source"),
        "baseline": info.get("baseline"),
    }
    if full:
        out["suite"] = suite
    return out
