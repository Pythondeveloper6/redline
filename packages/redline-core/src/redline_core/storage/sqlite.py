"""The default store: a separate SQLite file written by a background thread.

Design points (each one fixes something django-silk gets wrong):

* **Its own file** (``.redline/redline.db`` by default), never the application database –
  so profiling data cannot bloat it, and recording a query never records itself.
* **WAL mode**, so the UI can read while the writer writes.
* **Never blocks the request.** :meth:`SQLiteStore.save` only does ``put_nowait`` on a
  bounded queue. If the queue is full the trace is dropped and counted
  (:attr:`SQLiteStore.dropped`), rather than slowing the application down.
* A daemon **writer thread** drains the queue in batches, one transaction per batch, and
  prunes by age and row count every ``prune_every`` writes. It is started lazily, on the
  first :meth:`~SQLiteStore.save` in each process.
* **Fork-safe.** A store created before ``fork()`` (gunicorn ``--preload``, uWSGI without
  ``lazy-apps``, ``multiprocessing`` with the fork start method) notices it is in a new
  process and opens its own connection, queue and writer thread there. The parent's
  connection is never touched in the child.

One connection is shared behind a lock; SQLite serialises writers anyway and the reads the
UI does are small. ``path=":memory:"`` works (handy in tests).

Schema v2 (``CONTRACT.md`` §11) adds, in the same transaction as each batch of traces,
per-trace SQL / outbound HTTP / cache aggregates (pruned with their traces) and long-lived
history: hourly rollups with merged latency histograms, releases, an issue index, and the
load tester's runs. Those outlive traces for ``history_retention_s`` and survive
:meth:`~SQLiteStore.clear` unless ``history=True``. A v0.1 file is migrated (and the new
tables backfilled from its traces) the first time it is opened.
"""

from __future__ import annotations

import builtins
import dataclasses
import json
import logging
import os
import queue
import sqlite3
import threading
import time
import weakref
from collections import defaultdict
from typing import Any

from redline_core.diagnostics import capture_failure

from ..histogram import Histogram
from ..model import Callsite, Issue, Trace
from ..runs import Run, RunStep
from .base import (
    MAX_BUCKETS,
    Bottlenecks,
    CacheStats,
    EndpointStats,
    ExternalStats,
    Filters,
    History,
    IssueGroup,
    IssueIndexEntry,
    Page,
    Period,
    PeriodComparison,
    PeriodStats,
    ReleaseInfo,
    SqlStats,
    decode_cursor,
    encode_cursor,
    percentile,
)
from .checks import SCHEMA_V4, CheckStoreMixin
from .derive import HOUR, Derived, derive, fingerprint_key
from .private import ensure_private_dir, precreate, tighten

__all__ = ["SQLiteStore"]

logger = logging.getLogger("redline")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issue_suppressions (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    framework TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    route TEXT,
    view TEXT,
    status INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    query_count INTEGER NOT NULL,
    query_ms REAL NOT NULL,
    issue_count INTEGER NOT NULL,
    issue_kinds TEXT NOT NULL,
    has_profile INTEGER NOT NULL,
    tags TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_started ON traces (started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS traces_route ON traces (route, method);
CREATE TABLE IF NOT EXISTS profiles (
    trace_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trace_tags (
    trace_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trace_tags_kv ON trace_tags (key, value);
CREATE INDEX IF NOT EXISTS trace_tags_trace ON trace_tags (trace_id);
CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    started_at REAL NOT NULL,
    route TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS issues_by_id ON issues (issue_id, started_at);
CREATE INDEX IF NOT EXISTS issues_trace ON issues (trace_id);
CREATE INDEX IF NOT EXISTS issues_started ON issues (started_at);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lifecycle_hourly (
    bucket INTEGER NOT NULL,
    release TEXT NOT NULL,
    db_alias TEXT NOT NULL,
    vendor TEXT NOT NULL,
    connects_hour INTEGER NOT NULL,
    fresh_failures_hour INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bucket, release, db_alias, vendor)
);
CREATE INDEX IF NOT EXISTS lifecycle_bucket ON lifecycle_hourly (bucket DESC);
"""

#: v2 (``CONTRACT.md`` §11). Trace-scoped tables are pruned with their traces; the history
#: tables (rollups, releases, issue index, runs) are kept ``history_retention_s``.
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS trace_sql (
    trace_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    route TEXT NOT NULL,
    method TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    sql TEXT NOT NULL,
    count INTEGER NOT NULL,
    total_ms REAL NOT NULL,
    max_ms REAL NOT NULL,
    rows INTEGER,
    rows_n INTEGER NOT NULL DEFAULT 0,
    vendor TEXT NOT NULL DEFAULT 'other',
    db_alias TEXT NOT NULL DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS trace_sql_started ON trace_sql (started_at);
CREATE INDEX IF NOT EXISTS trace_sql_trace ON trace_sql (trace_id);
CREATE TABLE IF NOT EXISTS trace_ext (
    trace_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    method TEXT NOT NULL,
    host TEXT NOT NULL,
    url_pattern TEXT NOT NULL,
    count INTEGER NOT NULL,
    total_ms REAL NOT NULL,
    max_ms REAL NOT NULL,
    errors INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS trace_ext_started ON trace_ext (started_at);
CREATE INDEX IF NOT EXISTS trace_ext_trace ON trace_ext (trace_id);
CREATE TABLE IF NOT EXISTS trace_cache (
    trace_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    backend TEXT NOT NULL,
    key_pattern TEXT NOT NULL,
    gets INTEGER NOT NULL,
    hits INTEGER NOT NULL,
    misses INTEGER NOT NULL,
    sets INTEGER NOT NULL,
    deletes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS trace_cache_started ON trace_cache (started_at);
CREATE INDEX IF NOT EXISTS trace_cache_trace ON trace_cache (trace_id);
CREATE TABLE IF NOT EXISTS rollups (
    bucket INTEGER NOT NULL,
    release TEXT NOT NULL,
    route TEXT NOT NULL,
    method TEXT NOT NULL,
    count INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    sum_ms REAL NOT NULL,
    hist BLOB NOT NULL,
    sum_queries INTEGER NOT NULL,
    sum_query_ms REAL NOT NULL,
    issues INTEGER NOT NULL,
    PRIMARY KEY (bucket, release, route, method)
);
CREATE INDEX IF NOT EXISTS rollups_release ON rollups (release);
CREATE TABLE IF NOT EXISTS releases (
    release TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS issue_index (
    issue_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    route TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    first_release TEXT,
    last_release TEXT,
    occurrences INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS issue_index_last ON issue_index (last_seen);
CREATE TABLE IF NOT EXISTS issue_rollups (
    bucket INTEGER NOT NULL,
    release TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    occurrences INTEGER NOT NULL,
    PRIMARY KEY (bucket, release, issue_id)
);
CREATE INDEX IF NOT EXISTS issue_rollups_release ON issue_rollups (release);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    name TEXT NOT NULL,
    config TEXT NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_steps (
    run_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    target_rps REAL NOT NULL,
    achieved_rps REAL NOT NULL,
    sent INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    dropped INTEGER NOT NULL,
    hist BLOB NOT NULL,
    error_breakdown TEXT NOT NULL,
    PRIMARY KEY (run_id, step)
);
"""

SCHEMA_VERSION = 4
#: ``X-Redline-Run`` tags are kept for runs that ended at most this long ago (in-flight
#: requests of the last step).
_RUN_TAG_GRACE_S = 60.0
DEAD_RUN_MESSAGE = "the load process exited unexpectedly"
#: Tags set from ``X-Redline-Check`` (validated by the writer).
_CHECK_TAGS = ("check_run_id", "check_req", "check_n")
#: Tables holding one or more rows per trace, deleted with it.
_TRACE_TABLES = ("profiles", "trace_tags", "issues", "trace_sql", "trace_ext", "trace_cache")
#: Long-lived tables, kept across ``clear()`` unless ``history=True``.
_HISTORY_TABLES = ("rollups", "releases", "issue_index", "issue_rollups", "run_steps", "runs")

#: Every live store, so the fork hook can reset them in the child.
_STORES: weakref.WeakSet[SQLiteStore] = weakref.WeakSet()
#: Connections inherited across fork(). They must never be used *or closed* in the
#: child (closing would release the parent's locks), so they are kept alive here.
_INHERITED: list[Any] = []


def _after_fork_in_child() -> None:
    for store in list(_STORES):
        store._reset_after_fork()


if hasattr(os, "register_at_fork"):  # POSIX
    os.register_at_fork(after_in_child=_after_fork_in_child)

_SUMMARY_COLS = (
    "id, method, path, route, view, status, started_at, duration_ms, query_count, query_ms, "
    "issue_count, issue_kinds, has_profile, tags"
)


class SQLiteStore(CheckStoreMixin):
    """SQLite-backed :class:`~redline_core.storage.Store`.

    :param path: database file; parent directories are created. ``":memory:"`` works.
    :param background: write on a daemon thread (default). ``False`` writes inline,
        which tests and short scripts find convenient.
    :param queue_size: bound of the write queue; overflow is dropped and counted.
    :param max_rows, max_age_s: retention applied by :meth:`prune`, which the writer runs
        every ``prune_every`` saved traces.
    :param history_retention_s: how long the history tables (hourly rollups, releases, the
        issue index, load-test runs) are kept; independent of trace retention. ``0``/``None``
        keeps them forever.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] = ".redline/redline.db",
        *,
        background: bool = True,
        queue_size: int = 1_000,
        max_rows: int | None = 10_000,
        max_age_s: float | None = 7 * 86_400.0,
        prune_every: int = 200,
        batch_size: int = 50,
        history_retention_s: float | None = 90 * 86_400.0,
    ) -> None:
        path = os.fspath(path)
        for name, value in (
            ("max_rows", max_rows),
            ("max_age_s", max_age_s),
            ("history_retention_s", history_retention_s),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative (use 0 or None to disable it).")
        self.path = path
        #: Retention rules; ``0``/``None`` disables a rule.
        self.max_rows = max_rows or None
        self.max_age_s = max_age_s or None
        #: How long rollups, releases, the issue index and runs are kept (``None``: forever).
        self.history_retention_s = history_retention_s or None
        self.prune_every = max(1, prune_every)
        self.batch_size = max(1, batch_size)
        self.background = background
        self.queue_size = max(1, queue_size)
        self.dropped = 0
        self.written = 0
        self._since_prune = 0
        self._closed = False
        self._runs_cache: tuple[float, set[str]] | None = None
        self._check_cache: tuple[float, set[str]] | None = None
        self._last_lifecycle: tuple | None = None
        self._last_lifecycle_at = 0.0
        if path != ":memory:" and not path.startswith("file:"):
            # 0700 + a ".gitignore" of "*" when Redline creates the directory; the
            # database (and so its -wal/-shm files) is created 0600
            ensure_private_dir(os.path.dirname(os.path.abspath(path)))
            precreate(path)
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._conn = self._connect()
        self._queue: queue.Queue[Any] | None = None
        self._thread: threading.Thread | None = None
        self._pid = os.getpid()
        _STORES.add(self)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=10.0
        )
        conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.close()
                raise RuntimeError(
                    "Redline storage requires SQLite WAL on a compatible local filesystem"
                )
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        if self.path != ":memory:" and not self.path.startswith("file:"):
            tighten(self.path, f"{self.path}-wal", f"{self.path}-shm")  # older stores
        return conn

    def _reset_after_fork(self) -> None:
        """Called in a forked child: drop everything inherited from the parent.

        The parent's queue (and whatever was in it) and its writer thread do not exist
        here; the connection is parked, never used or closed. A fresh connection is opened
        and the writer starts on the next :meth:`save`. ``":memory:"`` stores start empty.
        """
        if self._pid == os.getpid():
            return
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._queue = None
        self._thread = None
        self.dropped = 0
        self.written = 0
        self._since_prune = 0
        _INHERITED.append(self._conn)
        if self._closed:
            return
        try:
            self._conn = self._connect()
        except Exception:  # pragma: no cover - e.g. the file vanished
            capture_failure("Redline could not reopen its store after fork")
            self._closed = True

    def _check_pid(self) -> None:
        # register_at_fork covers os.fork(); this also covers forks it cannot see
        # (a C extension calling fork() directly).
        if self._pid != os.getpid():
            self._reset_after_fork()

    def _ensure_writer(self) -> queue.Queue[Any]:
        q = self._queue
        if q is not None:
            return q
        with self._start_lock:
            if self._queue is None:
                self._queue = queue.Queue(maxsize=self.queue_size)
                self._thread = threading.Thread(
                    target=self._run, args=(self._queue,), name="redline-writer", daemon=True
                )
                self._thread.start()
            return self._queue

    @classmethod
    def from_settings(cls, settings: Any, **kwargs: Any) -> SQLiteStore:
        """Build from a :class:`~redline_core.conf.Settings`."""
        days = getattr(settings, "history_retention_days", 90)
        kwargs.setdefault("history_retention_s", float(days) * 86_400.0 if days else None)
        from .safety import prepare_path

        primary = cls(
            prepare_path(settings),
            queue_size=settings.writer_queue_size,
            max_rows=settings.max_rows,
            max_age_s=settings.max_age_s,
            **kwargs,
        )

        if settings.extra_stores:
            from .multiple import MultipleStores

            return MultipleStores(primary, settings.extra_stores)
        return primary

    # writing ---------------------------------------------------------------
    def save(self, trace: Trace) -> None:
        """Enqueue ``trace`` (never blocks); dropped and counted when the queue is full."""
        self._check_pid()
        if self._closed:
            self.dropped += 1
            return
        if not self.background:
            try:
                self._write([trace])
            except Exception:
                self.dropped += 1
                raise
            return
        try:
            self._ensure_writer().put_nowait(trace)
        except queue.Full:
            self.dropped += 1

    def flush(self, timeout: float | None = 5.0) -> bool:
        """Wait until everything enqueued so far is written. ``False`` on timeout."""
        self._check_pid()
        if self._queue is None or self._thread is None or not self._thread.is_alive():
            return True
        marker = threading.Event()
        try:
            self._queue.put(marker, timeout=timeout)
        except queue.Full:
            return False
        return marker.wait(timeout)

    def _run(self, q: queue.Queue[Any]) -> None:
        while True:
            item = q.get()
            batch: list[Trace] = []
            lifecycle: list[tuple] = []
            markers: list[threading.Event] = []
            stop = False
            while True:
                if item is None:
                    stop = True
                elif isinstance(item, threading.Event):
                    markers.append(item)
                elif isinstance(item, tuple) and item and item[0] == "lifecycle":
                    lifecycle.append(item)
                else:
                    batch.append(item)
                if stop or len(batch) + len(lifecycle) >= self.batch_size:
                    break
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
            if batch:
                try:
                    self._write(batch)
                except Exception as exc:  # keep the writer alive whatever happens
                    self.dropped += len(batch)
                    logger.error(
                        "Redline writer dropped %d trace(s) (%s)", len(batch), type(exc).__name__
                    )
            for _kind, rows, now in lifecycle:
                try:
                    self._write_lifecycle(rows, now)
                except Exception as exc:
                    logger.error("Redline writer dropped lifecycle gauges (%s)", type(exc).__name__)
            for m in markers:
                m.set()
            if stop:
                return

    def _known_runs(self) -> set[str]:
        """Ids of load runs that are running or ended less than a minute ago (re-read at
        most once a second)."""
        now = time.monotonic()
        cached = self._runs_cache
        if cached is not None and now - cached[0] < 1.0:
            return cached[1]
        try:
            rows = self._query(
                "SELECT id FROM runs WHERE status = 'running' OR finished_at >= ?",
                (time.time() - _RUN_TAG_GRACE_S,),
            )
            ids = {r["id"] for r in rows}
        except sqlite3.Error:  # pragma: no cover - e.g. a store being closed
            ids = set()
        self._runs_cache = (now, ids)
        return ids

    def _check_run_tags(self, traces: list[Trace]) -> list[Trace]:
        """Drop ``run_id``/``step`` tags (from ``X-Redline-Run``, which any client can send)
        unless the run exists in this store and is running (or just ended): a made-up
        header cannot attach traffic to a load run, or invent one."""
        if not any("run_id" in t.tags for t in traces):
            return traces
        known = self._known_runs()
        out = []
        for t in traces:
            if "run_id" in t.tags and t.tags["run_id"] not in known:
                tags = {k: v for k, v in t.tags.items() if k not in ("run_id", "step")}
                t = dataclasses.replace(t, tags=tags)
            out.append(t)
        return out

    def _check_check_tags(self, traces: list[Trace]) -> list[Trace]:
        """The ``X-Redline-Check`` rule (``CONTRACT.md`` §19.2): ``check_*`` tags stay only
        when that check run exists here and is running (or just ended).

        A trace the sampler would have dropped and kept only because it carried a check tag
        (``sampled_by == "check"``) is dropped altogether when the tag is not valid, so a
        made-up header cannot force anything into the store."""
        if not any("check_run_id" in t.tags for t in traces):
            return traces
        known = self.known_check_runs()
        out = []
        for t in traces:
            if "check_run_id" in t.tags and t.tags["check_run_id"] not in known:
                if t.tags.get("sampled_by") == "check":
                    continue
                tags = {k: v for k, v in t.tags.items() if k not in _CHECK_TAGS}
                t = dataclasses.replace(t, tags=tags)
            out.append(t)
        return out

    def _write(self, traces: list[Trace]) -> None:
        traces = self._check_check_tags(self._check_run_tags(traces))
        if not traces:
            return
        rows: builtins.list[tuple[Any, ...]] = []
        tags: builtins.list[tuple[str, str, str]] = []
        issues: builtins.list[tuple[Any, ...]] = []
        profiles: builtins.list[tuple[str, str]] = []
        for t in traces:
            body = json.dumps(t.to_dict(include_profile=False), default=str, ensure_ascii=False)
            rows.append(
                (
                    t.id,
                    t.started_at,
                    t.framework,
                    t.method,
                    t.path,
                    t.route,
                    t.view,
                    t.status,
                    t.duration_ms,
                    t.query_count,
                    t.query_ms,
                    len(t.issues),
                    "," + ",".join(t.issue_kinds) + "," if t.issues else "",
                    1 if t.profile is not None else 0,
                    json.dumps(t.tags),
                    body,
                )
            )
            tags.extend((t.id, k, v) for k, v in t.tags.items())
            route = f"{t.route or t.path}"
            for issue in t.issues:
                issues.append(
                    (issue.id, t.id, issue.kind, t.started_at, route, json.dumps(issue.to_dict()))
                )
            if t.profile is not None:
                profiles.append((t.id, json.dumps(t.profile)))
        derived = derive(traces)  # outside the lock; redone in the rare re-save case
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                # A re-saved trace replaces its rows; it is not counted in the history twice.
                ids = [t.id for t in traces]
                existing = {
                    r[0]
                    for r in c.execute(
                        f"SELECT id FROM traces WHERE id IN ({','.join('?' * len(ids))})", ids
                    )
                }
                if existing:
                    old = [(i,) for i in existing]
                    for table in _TRACE_TABLES[1:]:
                        c.executemany(f"DELETE FROM {table} WHERE trace_id = ?", old)
                    fresh = [t for t in traces if t.id not in existing]
                    derived = derive(fresh)
                    derive([t for t in traces if t.id in existing], history=False, out=derived)
                # Insertion order for the live tail: a counter in ``meta`` (never reused, even
                # after pruning), read and bumped inside this write transaction.
                seq = _next_seq(c, len(rows))
                rows = [(*row, seq + i) for i, row in enumerate(rows, 1)]
                c.executemany(
                    "INSERT OR REPLACE INTO traces VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                c.executemany("INSERT INTO trace_tags VALUES (?, ?, ?)", tags)
                c.executemany("INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?)", issues)
                c.executemany("INSERT OR REPLACE INTO profiles VALUES (?, ?)", profiles)
                _insert_derived(c, derived)
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        self.written += len(traces)
        self._since_prune += len(traces)
        if self._since_prune >= self.prune_every:
            self._since_prune = 0
            self.prune()

    # reading ---------------------------------------------------------------
    def _query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        self._check_pid()
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def get(self, trace_id: str, *, include_profile: bool = True) -> Trace | None:
        """The stored trace; its speedscope profile is loaded unless told not to."""
        rows = self._query("SELECT body, has_profile FROM traces WHERE id = ?", (trace_id,))
        if not rows:
            return None
        data = json.loads(rows[0]["body"])
        data.pop("has_profile", None)
        trace = Trace.from_dict(data)
        if include_profile and rows[0]["has_profile"]:
            trace.profile = self.get_profile(trace_id)
        return trace

    def get_profile(self, trace_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT data FROM profiles WHERE trace_id = ?", (trace_id,))
        return json.loads(rows[0]["data"]) if rows else None

    def has_profile(self, trace_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM profiles WHERE trace_id = ?", (trace_id,)))

    def count(self) -> int:
        return int(self._query("SELECT COUNT(*) FROM traces")[0][0])

    def list(
        self, filters: Filters | None = None, limit: int = 50, cursor: str | None = None
    ) -> Page:
        f = filters or Filters()
        limit = max(1, min(int(limit), 500))
        where, params = self._where(f)
        total = int(self._query(f"SELECT COUNT(*) FROM traces t{where}", params)[0][0])
        page_where, page_params = where, list(params)
        if cursor:
            started, last_id = decode_cursor(cursor)
            clause = "(t.started_at < ? OR (t.started_at = ? AND t.id < ?))"
            page_where = f"{where} AND {clause}" if where else f" WHERE {clause}"
            page_params += [started, started, last_id]
        rows = self._query(
            f"SELECT {_SUMMARY_COLS} FROM traces t{page_where} "
            "ORDER BY t.started_at DESC, t.id DESC LIMIT ?",
            [*page_params, limit + 1],
        )
        more = len(rows) > limit
        rows = rows[:limit]
        items = [_summary(r) for r in rows]
        next_cursor = encode_cursor(rows[-1]["started_at"], rows[-1]["id"]) if more else None
        return Page(items=items, next_cursor=next_cursor, total=total)

    @staticmethod
    def _where(f: Filters) -> tuple[str, builtins.list[Any]]:
        clauses: builtins.list[str] = []
        params: builtins.list[Any] = []
        if f.q:
            like = _like(f.q)
            clauses.append(
                "(lower(t.path) LIKE ? ESCAPE '\\' OR lower(coalesce(t.route, '')) LIKE ? "
                "ESCAPE '\\' OR lower(coalesce(t.view, '')) LIKE ? ESCAPE '\\' "
                "OR EXISTS (SELECT 1 FROM trace_tags g WHERE g.trace_id=t.id "
                "AND g.key IN ('request_id','user_id','job.id') "
                "AND lower(g.value) LIKE ? ESCAPE '\\'))"
            )
            params += [like, like, like, like]
        if f.method:
            clauses.append("t.method = ?")
            params.append(f.method)
        if f.status:
            if f.status.endswith("xx"):
                base = int(f.status[0]) * 100
                clauses.append("t.status >= ? AND t.status < ?")
                params += [base, base + 100]
            else:
                clauses.append("t.status = ?")
                params.append(int(f.status))
        if f.min_ms is not None:
            clauses.append("t.duration_ms >= ?")
            params.append(f.min_ms)
        if f.min_queries is not None:
            clauses.append("t.query_count >= ?")
            params.append(f.min_queries)
        if f.has_issues:
            clauses.append("t.issue_count > 0")
        if f.route:
            clauses.append("t.route = ?")
            params.append(f.route)
        if f.tag:
            clauses.append(
                "EXISTS (SELECT 1 FROM trace_tags g WHERE g.trace_id = t.id AND g.key = ? "
                "AND g.value = ?)"
            )
            params += list(f.tag)
        if f.replay_of:
            clauses.append(
                "EXISTS (SELECT 1 FROM trace_tags g WHERE g.trace_id = t.id "
                "AND g.key = 'replay_of' AND g.value = ?)"
            )
            params.append(f.replay_of.lower())
        if f.since is not None:
            clauses.append("t.started_at > ?")
            params.append(f.since)
        if f.sql:
            clauses.append(
                "EXISTS (SELECT 1 FROM trace_sql s WHERE s.trace_id = t.id "
                "AND lower(s.sql) LIKE ? ESCAPE '\\')"
            )
            params.append(_like(f.sql))
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def endpoints(self, window: float, now: float | None = None) -> builtins.list[EndpointStats]:
        """Per ``(route, method)`` latency percentiles, query stats and a ≤48-bucket series.

        Requests without a resolved route are grouped by their concrete path. ``series``
        lists only non-empty buckets, ``t`` being each bucket's start (epoch seconds).
        """
        now = time.time() if now is None else now
        since = now - window
        rows = self._query(
            "SELECT coalesce(route, path) AS r, method, status, duration_ms, query_count, "
            "query_ms, issue_count, started_at FROM traces WHERE started_at >= ? "
            "AND started_at <= ?",
            (since, now),
        )
        groups: dict[tuple[str, str], builtins.list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            groups[(row["r"], row["method"])].append(row)
        bucket_s = window / MAX_BUCKETS
        out = []
        for (route, method), items in groups.items():
            durations = sorted(r["duration_ms"] for r in items)
            n = len(items)
            buckets: dict[int, builtins.list[float]] = defaultdict(list)
            for r in items:
                idx = min(MAX_BUCKETS - 1, max(0, int((r["started_at"] - since) / bucket_s)))
                buckets[idx].append(r["duration_ms"])
            series = [
                {
                    "t": round(since + idx * bucket_s, 3),
                    "p95_ms": round(percentile(sorted(vals), 95), 3),
                    "count": len(vals),
                }
                for idx, vals in sorted(buckets.items())
            ]
            out.append(
                EndpointStats(
                    route=route,
                    method=method,
                    count=n,
                    error_rate=round(sum(1 for r in items if r["status"] >= 500) / n, 4),
                    p50_ms=round(percentile(durations, 50), 3),
                    p95_ms=round(percentile(durations, 95), 3),
                    p99_ms=round(percentile(durations, 99), 3),
                    avg_queries=round(sum(r["query_count"] for r in items) / n, 2),
                    max_queries=max(r["query_count"] for r in items),
                    avg_query_ms=round(sum(r["query_ms"] for r in items) / n, 3),
                    issue_count=sum(r["issue_count"] for r in items),
                    series=series,
                )
            )
        out.sort(key=lambda e: (-e.count, -e.p95_ms, e.route))
        return out

    def list_suppressions(self) -> builtins.list[dict[str, Any]]:
        return [
            json.loads(row["data"])
            for row in self._query("SELECT data FROM issue_suppressions ORDER BY id", ())
        ]

    def save_suppression(self, rule: dict[str, Any]) -> None:
        self._write_now(
            [
                (
                    "INSERT INTO issue_suppressions (id, data) VALUES (?, ?)",
                    (rule["id"], json.dumps(rule)),
                )
            ]
        )

    def delete_suppression(self, rule_id: str) -> None:
        self._write_now([("DELETE FROM issue_suppressions WHERE id = ?", (rule_id,))])

    def issues(
        self, window: float, kind: str | None = None, now: float | None = None
    ) -> builtins.list[IssueGroup]:
        """Issues deduplicated by id; the sample is the most recent occurrence."""
        now = time.time() if now is None else now
        params: builtins.list[Any] = [now - window, now]
        extra = ""
        if kind:
            extra = " AND kind = ?"
            params.append(kind)
        rows = self._query(
            "SELECT issue_id, trace_id, started_at, route, data FROM issues "
            f"WHERE started_at >= ? AND started_at <= ?{extra} ORDER BY started_at ASC",
            params,
        )
        acc: dict[str, dict[str, Any]] = {}
        for row in rows:
            g = acc.get(row["issue_id"])
            if g is None:
                g = acc[row["issue_id"]] = {
                    "first": row["started_at"],
                    "n": 0,
                    "observed_total_ms": 0.0,
                    "routes": [],
                }
            g["n"] += 1
            g["observed_total_ms"] += float(json.loads(row["data"]).get("total_ms") or 0)
            g["last"] = row["started_at"]
            g["trace"] = row["trace_id"]
            g["data"] = row["data"]
            if row["route"] not in g["routes"]:
                g["routes"].append(row["route"])
        order = {"critical": 0, "warning": 1, "info": 2}
        out = []
        for iid, g in acc.items():
            issue = Issue.from_dict(json.loads(g["data"]))
            out.append(
                IssueGroup(
                    id=iid,
                    kind=issue.kind,
                    severity=issue.severity,
                    title=issue.title,
                    detail=issue.detail,
                    occurrences=g["n"],
                    observed_total_ms=g["observed_total_ms"],
                    fingerprint=issue.fingerprint,
                    first_seen=g["first"],
                    last_seen=g["last"],
                    routes=g["routes"],
                    sample_trace_id=g["trace"],
                    sample_span_ids=issue.span_ids,
                    suggestion=issue.suggestion,
                    callsite=issue.callsite,
                )
            )
        out.sort(key=lambda i: (order.get(i.severity, 3), -i.occurrences, -i.last_seen))
        return out

    # deleting --------------------------------------------------------------
    def _delete_ids(self, ids: builtins.list[str]) -> int:
        if not ids:
            return 0
        self._check_pid()
        params = [(i,) for i in ids]
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                for table in _TRACE_TABLES:
                    c.executemany(f"DELETE FROM {table} WHERE trace_id = ?", params)
                c.executemany("DELETE FROM traces WHERE id = ?", params)
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return len(ids)

    def delete(self, trace_id: str) -> int:
        exists = self._query("SELECT id FROM traces WHERE id = ?", (trace_id,))
        return self._delete_ids([r["id"] for r in exists])

    def clear(self, history: bool = False) -> int:
        """Delete every trace; returns how many.

        The history (hourly rollups, releases, the issue index, load-test runs) outlives
        traces by design and is kept; ``history=True`` wipes it too.
        """
        self._check_pid()
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                n = int(c.execute("SELECT COUNT(*) FROM traces").fetchone()[0])
                for table in (*_TRACE_TABLES, "traces", *(_HISTORY_TABLES if history else ())):
                    c.execute(f"DELETE FROM {table}")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return n

    def prune(self, max_age_s: float | None = None, max_rows: int | None = None) -> int:
        """Delete traces older than ``max_age_s`` and the oldest beyond ``max_rows``.

        ``None`` uses the store's own rule; ``0`` disables that rule for this call (it
        never means "delete everything" – use :meth:`clear` for that). Negative values
        raise :class:`ValueError`.
        """
        for name, value in (("max_age_s", max_age_s), ("max_rows", max_rows)):
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative (use 0 or None to disable it).")
        max_age_s = self.max_age_s if max_age_s is None else max_age_s
        max_rows = self.max_rows if max_rows is None else max_rows
        doomed: builtins.list[str] = []
        if max_age_s:
            cutoff = time.time() - max_age_s
            doomed += [
                r["id"]
                for r in self._query("SELECT id FROM traces WHERE started_at < ?", (cutoff,))
            ]
        if max_rows:
            rows = self._query(
                "SELECT id FROM traces ORDER BY started_at DESC, id DESC LIMIT -1 OFFSET ?",
                (max_rows,),
            )
            seen = set(doomed)
            doomed += [r["id"] for r in rows if r["id"] not in seen]
        deleted = self._delete_ids(doomed)
        self.prune_history()
        return deleted

    def prune_history(self, retention_s: float | None = None, now: float | None = None) -> int:
        """Drop history older than ``retention_s`` (default: the store's
        ``history_retention_s``; ``None``/``0`` there keeps it forever).

        Hourly rollups whose hour ended before the cutoff, releases and issue-index entries
        last seen before it, and finished runs created before it go. Running runs are kept.
        Called by :meth:`prune`; returns the number of rows deleted.
        """
        if retention_s is not None and retention_s < 0:
            raise ValueError("retention_s must not be negative.")
        retention_s = self.history_retention_s if retention_s is None else retention_s
        if not retention_s:
            return 0
        cutoff = (time.time() if now is None else now) - retention_s
        self._check_pid()
        n = 0
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                for sql in (
                    "DELETE FROM rollups WHERE bucket + 3600 <= ?",
                    "DELETE FROM issue_rollups WHERE bucket + 3600 <= ?",
                    "DELETE FROM lifecycle_hourly WHERE bucket + 3600 <= ?",
                    "DELETE FROM releases WHERE last_seen < ?",
                    "DELETE FROM issue_index WHERE last_seen < ?",
                    "DELETE FROM run_steps WHERE run_id IN (SELECT id FROM runs WHERE "
                    "created_at < ? AND status != 'running')",
                    "DELETE FROM runs WHERE created_at < ? AND status != 'running'",
                ):
                    n += c.execute(sql, (cutoff,)).rowcount
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return n

    # analytics (v0.2) ----------------------------------------------------------
    def _sql_groups(
        self, since: float, until: float, q: str | None = None
    ) -> builtins.list[SqlStats]:
        where = "started_at >= ? AND started_at <= ?"
        params: builtins.list[Any] = [since, until]
        if q:
            where += " AND lower(sql) LIKE ? ESCAPE '\\'"
            params.append(_like(q))
        rows = self._query(
            "SELECT fingerprint, max(sql) AS sql, sum(count) AS n, count(DISTINCT trace_id) "
            "AS traces, sum(total_ms) AS total, max(max_ms) AS mx, sum(rows) AS rows, "
            "sum(rows_n) AS rows_n, max(vendor) AS vendor "
            f"FROM trace_sql WHERE {where} GROUP BY fingerprint",
            params,
        )
        # SQLite: a bare column next to max() comes from the row holding the maximum.
        samples = {
            r[0]: r[1]
            for r in self._query(
                "SELECT fingerprint, trace_id, max(started_at) FROM trace_sql "
                f"WHERE {where} GROUP BY fingerprint",
                params,
            )
        }
        routes: dict[str, builtins.list[str]] = defaultdict(list)
        for r in self._query(
            f"SELECT DISTINCT fingerprint, method, route FROM trace_sql WHERE {where}", params
        ):
            if len(routes[r[0]]) < 20:
                routes[r[0]].append(f"{r[1]} {r[2]}")
        return [
            SqlStats(
                fingerprint=r["fingerprint"],
                sql=r["sql"],
                count=int(r["n"]),
                traces=int(r["traces"]),
                total_ms=float(r["total"]),
                max_ms=float(r["mx"]),
                routes=sorted(routes[r["fingerprint"]]),
                sample_trace_id=samples.get(r["fingerprint"], ""),
                vendor=r["vendor"] or "other",
                mean_rows=(r["rows"] / r["rows_n"]) if r["rows_n"] else None,
            )
            for r in rows
        ]

    def search_sql(
        self, q: str | None, window: float, limit: int = 100, now: float | None = None
    ) -> builtins.list[SqlStats]:
        """Fingerprints whose normalised SQL contains ``q`` (all when empty), by total time."""
        now = time.time() if now is None else now
        out = self._sql_groups(now - window, now, q)
        out.sort(key=lambda s: (-s.total_ms, -s.count, s.fingerprint))
        return out[: max(1, int(limit))]

    def sql_workload(self, window: float, now: float | None = None) -> builtins.list[SqlStats]:
        """Every fingerprint seen in ``window`` (what the index advisor reads), by total time."""
        now = time.time() if now is None else now
        out = self._sql_groups(now - window, now)
        out.sort(key=lambda s: (-s.total_ms, -s.count, s.fingerprint))
        return out

    def cache_stats(self, window: float, now: float | None = None) -> builtins.list[CacheStats]:
        """Per ``(backend, key_pattern)`` reads, hit rate, unread sets and stampedes."""
        now = time.time() if now is None else now
        params = (now - window, now)
        rows = self._query(
            "SELECT backend, key_pattern, sum(gets) g, sum(hits) h, sum(misses) m, sum(sets) s, "
            "sum(deletes) d FROM trace_cache WHERE started_at >= ? AND started_at <= ? "
            "GROUP BY backend, key_pattern",
            params,
        )
        misses: dict[tuple[str, str], builtins.list[float]] = defaultdict(list)
        for r in self._query(
            "SELECT backend, key_pattern, started_at FROM trace_cache WHERE started_at >= ? "
            "AND started_at <= ? AND misses > 0 ORDER BY started_at",
            params,
        ):
            misses[(r[0], r[1])].append(r[2])
        out = []
        for r in rows:
            gets, hits, miss, sets = int(r["g"]), int(r["h"]), int(r["m"]), int(r["s"])
            key = (r["backend"], r["key_pattern"])
            out.append(
                CacheStats(
                    backend=key[0],
                    key_pattern=key[1],
                    gets=gets,
                    hits=hits,
                    misses=miss,
                    hit_rate=hits / (hits + miss) if hits + miss else None,
                    sets=sets,
                    deletes=int(r["d"]),
                    unread_sets=sets if gets == 0 else 0,
                    stampedes=_bursts(misses.get(key, ()), 3, 1.0),
                )
            )
        out.sort(key=lambda c: (-(c.gets + c.sets), c.backend, c.key_pattern))
        return out

    def external_stats(
        self, window: float, now: float | None = None
    ) -> builtins.list[ExternalStats]:
        """Outbound calls per ``(method, url_pattern)``: latency, errors, duplicates, retries."""
        now = time.time() if now is None else now
        groups: dict[tuple[str, str, str], builtins.list[Any]] = {}
        for r in self._query(
            "SELECT method, host, url_pattern, count, total_ms, max_ms, errors FROM trace_ext "
            "WHERE started_at >= ? AND started_at <= ?",
            (now - window, now),
        ):
            key = (r[0], r[1], r[2])
            g = groups.get(key)
            if g is None:
                g = groups[key] = [0, 0.0, 0.0, 0, 0, 0, Histogram()]
            n, total = int(r[3]), float(r[4])
            g[0] += n
            g[1] += total
            g[2] = max(g[2], float(r[5]))
            g[3] += int(r[6])
            if n >= 2:
                g[4] += 1
            if n >= 3 and r[6] >= 1:
                g[5] += 1
            g[6].record(total / n if n else 0.0, n)
        out = [
            ExternalStats(
                method=m,
                host=host,
                url_pattern=url,
                count=g[0],
                total_ms=g[1],
                p95_ms=g[6].percentile(95),
                max_ms=g[2],
                errors=g[3],
                duplicates=g[4],
                retries=g[5],
            )
            for (m, host, url), g in groups.items()
        ]
        out.sort(key=lambda e: (-e.total_ms, e.url_pattern))
        return out

    # history (v0.2) ------------------------------------------------------------
    def record_lifecycle(self, metrics, *, release=None):
        """Persist per-process hourly connection gauges as max-per-hour rows.

        The recorder calls this on trace completion; identical gauges within a minute are
        skipped so a busy endpoint does not add a write per request. ``MAX`` keeps the
        highest observed count, a lower bound when several processes share a release.
        """
        if not metrics or self._closed:
            return
        now = time.time()
        bucket = int(now // 3600) * 3600
        rows = []
        for metric in list(metrics)[:16]:
            alias = str(metric.get("alias", "default"))[:64]
            vendor = str(metric.get("vendor", "other"))[:32]
            connects = max(0, int(metric.get("connects_hour", 0)))
            failures = max(0, int(metric.get("fresh_failures_hour", 0)))
            if not connects and not failures:
                continue
            rows.append(
                (bucket, str(release or "unknown")[:128], alias, vendor, connects, failures)
            )
        if not rows:
            return
        signature = (bucket, tuple(sorted(rows)))
        if signature == self._last_lifecycle and now - self._last_lifecycle_at < 60:
            return
        self._last_lifecycle, self._last_lifecycle_at = signature, now
        if self.background:
            try:
                self._ensure_writer().put_nowait(("lifecycle", rows, now))
            except queue.Full:
                self.dropped += 1
            return
        self._write_lifecycle(rows, now)

    def _write_lifecycle(self, rows, now):
        self._check_pid()
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                c.executemany(
                    """
                    INSERT INTO lifecycle_hourly
                        (bucket, release, db_alias, vendor, connects_hour,
                         fresh_failures_hour, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bucket, release, db_alias, vendor) DO UPDATE SET
                        connects_hour = MAX(lifecycle_hourly.connects_hour,
                                            excluded.connects_hour),
                        fresh_failures_hour = MAX(lifecycle_hourly.fresh_failures_hour,
                                                  excluded.fresh_failures_hour),
                        updated_at = excluded.updated_at
                    """,
                    [(*row, now) for row in rows],
                )
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def lifecycle(self, window, limit=200):
        """Hourly connection gauges inside ``window`` seconds, newest first."""
        if window < 0:
            raise ValueError("window must not be negative")
        since = int((time.time() - window) // 3600) * 3600
        limit = max(1, min(int(limit), 500))
        self._check_pid()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT bucket, release, db_alias, vendor, connects_hour,
                       fresh_failures_hour, updated_at
                FROM lifecycle_hourly
                WHERE bucket >= ?
                ORDER BY bucket DESC, db_alias, vendor
                LIMIT ?
                """,
                (since, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def history(
        self,
        route: str | None,
        method: str | None,
        start: float,
        end: float,
        bucket: float = 3_600.0,
    ) -> History:
        """The hourly rollups between ``start`` and ``end`` merged into ``bucket``-second
        buckets (``3600`` or ``86400``; UTC-aligned). ``route=None`` is the whole app."""
        size = max(HOUR, int(bucket) // HOUR * HOUR)
        where = ["bucket >= ?", "bucket < ?"]
        params: builtins.list[Any] = [int(start // HOUR) * HOUR, end]
        if route:
            where.append("route = ?")
            params.append(route)
        if method:
            where.append("method = ?")
            params.append(method.upper())
        merged: dict[int, builtins.list[Any]] = {}
        for r in self._query(
            "SELECT bucket, count, errors, hist, sum_queries FROM rollups WHERE "
            + " AND ".join(where),
            params,
        ):
            t = int(r[0]) // size * size
            g = merged.get(t)
            if g is None:
                g = merged[t] = [0, 0, Histogram(), 0]
            g[0] += r[1]
            g[1] += r[2]
            _merge_blob(g[2], r[3])
            g[3] += r[4]
        series = [
            {
                "t": t,
                "count": g[0],
                "p50_ms": round(g[2].percentile(50), 3),
                "p95_ms": round(g[2].percentile(95), 3),
                "p99_ms": round(g[2].percentile(99), 3),
                "error_rate": round(g[1] / g[0], 4) if g[0] else 0.0,
                "avg_queries": round(g[3] / g[0], 2) if g[0] else 0.0,
            }
            for t, g in sorted(merged.items())
        ]
        releases = [
            {"release": r[0], "first_seen": r[1]}
            for r in self._query(
                "SELECT release, first_seen FROM releases WHERE first_seen >= ? "
                "AND first_seen < ? ORDER BY first_seen",
                (start, end),
            )
        ]
        return History(series=series, releases=releases)

    def releases(self) -> builtins.list[ReleaseInfo]:
        """Every release seen (newest first)."""
        return [
            ReleaseInfo(release=r[0], first_seen=r[1], last_seen=r[2], requests=r[3])
            for r in self._query(
                "SELECT release, first_seen, last_seen, requests FROM releases "
                "ORDER BY first_seen DESC"
            )
        ]

    @staticmethod
    def _period_where(period: Period) -> tuple[str, builtins.list[Any]]:
        if period[0] == "release":
            return "release = ?", [period[1]]
        if period[0] == "range":
            start, end = float(period[1]), float(period[2])  # type: ignore[misc]
            return "bucket >= ? AND bucket < ?", [int(start // HOUR) * HOUR, end]
        raise ValueError(f"Unknown period {period!r}")

    def _period_stats(self, period: Period) -> dict[tuple[str, str], PeriodStats]:
        where, params = self._period_where(period)
        acc: dict[tuple[str, str], builtins.list[Any]] = {}
        for r in self._query(
            "SELECT route, method, count, errors, sum_ms, hist, sum_queries FROM rollups "
            f"WHERE {where}",
            params,
        ):
            g = acc.get((r[0], r[1]))
            if g is None:
                g = acc[(r[0], r[1])] = [0, 0, 0.0, Histogram(), 0]
            g[0] += r[2]
            g[1] += r[3]
            g[2] += r[4]
            _merge_blob(g[3], r[5])
            g[4] += r[6]
        return {
            key: PeriodStats(
                count=g[0],
                error_rate=g[1] / g[0] if g[0] else 0.0,
                p50_ms=g[3].percentile(50),
                p95_ms=g[3].percentile(95),
                p99_ms=g[3].percentile(99),
                avg_ms=g[2] / g[0] if g[0] else 0.0,
                avg_queries=g[4] / g[0] if g[0] else 0.0,
            )
            for key, g in acc.items()
            if g[0]
        }

    def _period_issues(self, period: Period) -> dict[str, int]:
        where, params = self._period_where(period)
        return {
            r[0]: int(r[1])
            for r in self._query(
                f"SELECT issue_id, sum(occurrences) FROM issue_rollups WHERE {where} "
                "GROUP BY issue_id",
                params,
            )
        }

    def compare_periods(self, a: Period, b: Period) -> PeriodComparison:
        """Endpoints and issues of period ``b`` against baseline ``a`` (``CONTRACT.md`` §12).

        Verdicts: ``new``/``gone`` when an endpoint has traffic in only one period;
        ``regressed`` when p95 rose ≥ 20 % *and* ≥ 5 ms, or average queries rose by ≥ 1;
        ``improved`` for the mirror image; else ``same``. Ranges have hourly resolution.
        """
        sa, sb = self._period_stats(a), self._period_stats(b)
        endpoints = []
        for key in set(sa) | set(sb):
            pa, pb = sa.get(key), sb.get(key)
            delta_pct = delta_q = None
            if pa is None:
                verdict = "new"
            elif pb is None:
                verdict = "gone"
            else:
                d_p95 = pb.p95_ms - pa.p95_ms
                delta_q = round(pb.avg_queries - pa.avg_queries, 2)
                delta_pct = round(d_p95 / pa.p95_ms * 100, 1) if pa.p95_ms else None
                if (d_p95 >= 5 and pb.p95_ms >= pa.p95_ms * 1.2) or delta_q >= 1:
                    verdict = "regressed"
                elif (-d_p95 >= 5 and pa.p95_ms >= pb.p95_ms * 1.2) or delta_q <= -1:
                    verdict = "improved"
                else:
                    verdict = "same"
            endpoints.append(
                {
                    "route": key[0],
                    "method": key[1],
                    "a": pa.to_dict() if pa else None,
                    "b": pb.to_dict() if pb else None,
                    "delta_p95_pct": delta_pct,
                    "delta_queries": delta_q,
                    "verdict": verdict,
                }
            )
        order = {"regressed": 0, "new": 1, "improved": 2, "gone": 3, "same": 4}
        endpoints.sort(
            key=lambda e: (
                order[e["verdict"]],
                -((e["b"] or e["a"])["count"]),
                e["route"],
                e["method"],
            )
        )
        ia, ib = self._period_issues(a), self._period_issues(b)
        meta = self._issue_meta(set(ia) | set(ib))

        def items(ids: Any, counts: dict[str, int]) -> builtins.list[dict[str, Any]]:
            out = [{**meta[i], "occurrences": counts[i]} for i in ids if i in meta]
            out.sort(key=lambda x: (-x["occurrences"], x["id"]))
            return out

        issues = {
            "new": items(set(ib) - set(ia), ib),
            "resolved": items(set(ia) - set(ib), ia),
            "persisting": items(set(ia) & set(ib), ib),
        }
        return PeriodComparison(
            a=_period_dict(a, sa), b=_period_dict(b, sb), endpoints=endpoints, issues=issues
        )

    def _issue_meta(self, ids: set[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        id_list = sorted(ids)
        for i in range(0, len(id_list), 500):
            chunk = id_list[i : i + 500]
            for r in self._query(
                f"SELECT * FROM issue_index WHERE issue_id IN ({','.join('?' * len(chunk))})",
                chunk,
            ):
                out[r["issue_id"]] = _index_entry(r).to_dict()
        return out

    def issue_index(
        self, window: float | None = None, now: float | None = None
    ) -> builtins.list[IssueIndexEntry]:
        """Every issue ever indexed (or last seen within ``window``), most recent first."""
        where, params = "", []
        if window:
            now = time.time() if now is None else now
            where, params = " WHERE last_seen >= ?", [now - window]
        return [
            _index_entry(r)
            for r in self._query(
                f"SELECT * FROM issue_index{where} ORDER BY last_seen DESC, issue_id", params
            )
        ]

    # load-test runs (v0.2) -----------------------------------------------------
    def _write_now(self, statements: builtins.list[tuple[str, Any]]) -> None:
        self._check_pid()
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                for sql, params in statements:
                    c.execute(sql, params)
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def save_run(self, run: Run) -> None:
        """Insert or replace ``run`` and its steps (written immediately, not queued)."""
        statements: builtins.list[tuple[str, Any]] = [
            (
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    run.created_at,
                    run.finished_at,
                    run.status,
                    run.name,
                    json.dumps(run.config, default=str),
                    json.dumps(run.summary or {}, default=str),
                ),
            )
        ]
        statements += [_step_statement(run.id, s) for s in run.steps]
        self._write_now(statements)

    def save_run_step(self, run_id: str, step: RunStep) -> None:
        """Insert or replace one step (call it again as the step progresses)."""
        self._write_now([_step_statement(run_id, step)])

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any] | None = None) -> None:
        """Mark a run ``done``/``stopped``/``failed`` and store its summary."""
        self._write_now(
            [
                (
                    "UPDATE runs SET status = ?, finished_at = ?, summary = ? WHERE id = ?",
                    (status, time.time(), json.dumps(summary or {}, default=str), run_id),
                )
            ]
        )

    #: A ``running`` run younger than this is never judged dead (its process is starting).
    RUN_GRACE_S = 10.0

    def _reap_dead_runs(self) -> None:
        """Mark ``running`` rows whose process is gone as ``failed`` (and persist it).

        Only applies where runs are process-managed: a ``runs`` directory beside the store
        file holds ``<run id>.pid``. A row older than :attr:`RUN_GRACE_S` with a missing or
        dead pid (the load process was killed or never started) can never finish.
        """
        if self.path == ":memory:" or self.path.startswith("file:"):
            return
        if not os.path.isdir(os.path.join(os.path.dirname(os.path.abspath(self.path)), "runs")):
            return
        rows = self._query(
            "SELECT id FROM runs WHERE status = 'running' AND created_at < ?",
            (time.time() - self.RUN_GRACE_S,),
        )
        if not rows:
            return
        from ..load.control import run_process_alive

        for r in rows:
            try:
                alive = run_process_alive(r["id"], self.path)
            except ValueError:  # not a valid run id: nothing to look up
                alive = False
            if not alive:
                self._write_now(
                    [
                        (
                            "UPDATE runs SET status = 'failed', finished_at = ?, summary = ? "
                            "WHERE id = ? AND status = 'running'",
                            (
                                time.time(),
                                json.dumps({"message": DEAD_RUN_MESSAGE}),
                                r["id"],
                            ),
                        )
                    ]
                )

    def get_run(self, run_id: str) -> Run | None:
        self._reap_dead_runs()
        rows = self._query("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not rows:
            return None
        steps = self._query("SELECT * FROM run_steps WHERE run_id = ? ORDER BY step", (run_id,))
        return _run(rows[0], [_run_step(s) for s in steps])

    def list_runs(self, limit: int = 50) -> builtins.list[Run]:
        """The most recent runs first, each with its steps."""
        self._reap_dead_runs()
        rows = self._query(
            "SELECT * FROM runs ORDER BY created_at DESC, id LIMIT ?", (max(1, int(limit)),)
        )
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        steps: dict[str, builtins.list[RunStep]] = defaultdict(list)
        for s in self._query(
            f"SELECT * FROM run_steps WHERE run_id IN ({','.join('?' * len(ids))}) ORDER BY step",
            ids,
        ):
            steps[s["run_id"]].append(_run_step(s))
        return [_run(r, steps[r["id"]]) for r in rows]

    def delete_run(self, run_id: str) -> int:
        """Delete a run and its steps (its tagged traces stay); returns 1 or 0."""
        self._check_pid()
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("DELETE FROM run_steps WHERE run_id = ?", (run_id,))
                n = c.execute("DELETE FROM runs WHERE id = ?", (run_id,)).rowcount
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return n

    def run_bottlenecks(self, run_id: str, limit: int = 50) -> Bottlenecks | None:
        """Per step, the queries (``trace_sql``) and routes of the traces the run tagged.

        ``mean_ms`` is per execution (per request for routes), ``p95_ms`` the 95th percentile
        of each request's mean, ``count`` executions (requests). ``growth`` is the mean in the
        last step the item appears in over the mean in the first (the first floored at
        0.01 ms). ``None`` when there is neither such a run nor any trace tagged with it.
        """
        steps_rows = self._query(
            "SELECT step, target_rps FROM run_steps WHERE run_id = ? ORDER BY step", (run_id,)
        )
        tagged = (
            "FROM trace_tags r JOIN trace_tags s ON s.trace_id = r.trace_id AND s.key = 'step' "
            "{join} WHERE r.key = 'run_id' AND r.value = ?"
        )
        # key -> step -> [count, total_ms, per-request means, sample trace]
        acc: dict[tuple[str, str], dict[int, builtins.list[Any]]] = defaultdict(dict)
        labels: dict[tuple[str, str], str] = {}
        for r in self._query(
            "SELECT s.value, q.fingerprint, q.sql, q.count, q.total_ms, q.trace_id "
            + tagged.format(join="JOIN trace_sql q ON q.trace_id = r.trace_id"),
            (run_id,),
        ):
            step = _int_or_none(r[0])
            if step is None:
                continue
            key = ("sql", r[1])
            labels[key] = r[2]
            _add_sample(acc[key], step, int(r[3]), float(r[4]), r[5])
        for r in self._query(
            "SELECT s.value, t.method, coalesce(t.route, t.path), t.duration_ms, t.id "
            + tagged.format(join="JOIN traces t ON t.id = r.trace_id"),
            (run_id,),
        ):
            step = _int_or_none(r[0])
            if step is None:
                continue
            key = ("route", f"{r[1]} {r[2]}")
            labels[key] = key[1]
            _add_sample(acc[key], step, 1, float(r[3]), r[4])
        if not steps_rows and not acc:
            if not self._query("SELECT 1 FROM runs WHERE id = ?", (run_id,)):
                return None
        steps = [{"step": int(r[0]), "target_rps": r[1]} for r in steps_rows]
        if not steps:  # no step rows stored: derive them from the tags
            seen = sorted({s for per in acc.values() for s in per})
            steps = [{"step": s, "target_rps": None} for s in seen]
        items = []
        for (kind, ident), per in acc.items():
            per_step = []
            for step in sorted(per):
                count, total, means, _ = per[step]
                per_step.append(
                    {
                        "step": step,
                        "mean_ms": round(total / count, 3) if count else 0.0,
                        "p95_ms": round(percentile(sorted(means), 95), 3),
                        "count": count,
                    }
                )
            first, last = per_step[0]["mean_ms"], per_step[-1]["mean_ms"]
            items.append(
                {
                    "key": f"{kind}:{ident}",
                    "kind": kind,
                    "label": labels[(kind, ident)][:200],
                    "callsite": None,
                    "per_step": per_step,
                    "growth": round(last / max(first, 0.01), 2) if len(per_step) > 1 else 1.0,
                    "_sample": per[max(per)][3],
                    "_fp": ident if kind == "sql" else None,
                }
            )
        items.sort(key=lambda i: (-i["growth"], -i["per_step"][-1]["mean_ms"], i["key"]))
        items = items[: max(1, limit)]
        for item in items:
            sample, fp = item.pop("_sample"), item.pop("_fp")
            if fp is not None:
                cs = self._callsite_for(sample, fp)
                item["callsite"] = cs.to_dict() if cs else None
        return Bottlenecks(steps=steps, items=items)

    def _callsite_for(self, trace_id: str, fp_key: str) -> Callsite | None:
        trace = self.get(trace_id, include_profile=False)
        if trace is None:
            return None
        for span in trace.spans:
            if span.kind == "sql" and span.callsite is not None:
                fp = span.data.get("fingerprint")
                if fp and fingerprint_key(fp) == fp_key:
                    return span.callsite
        return None

    def traces_since(
        self,
        started_at: float,
        trace_id: str = "",
        limit: int = 100,
        after_seq: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Summaries of newly saved traces, oldest *saved* first (live tail).

        Without ``after_seq``: traces after ``(started_at, trace_id)`` (the initial
        position, an epoch). With it: traces saved after that ``seq`` (every summary here
        carries its ``seq``), in insertion order, so a slow request that started before
        already-streamed fast ones is still delivered when it finishes.
        """
        limit = max(1, min(int(limit), 500))
        if after_seq is not None:
            rows = self._query(
                f"SELECT {_SUMMARY_COLS}, saved_seq FROM traces WHERE saved_seq > ? "
                "ORDER BY saved_seq ASC LIMIT ?",
                (int(after_seq), limit),
            )
        else:
            rows = self._query(
                f"SELECT {_SUMMARY_COLS}, saved_seq FROM traces WHERE started_at > ? OR "
                "(started_at = ? AND id > ?) ORDER BY saved_seq ASC LIMIT ?",
                (started_at, started_at, trace_id or "", limit),
            )
        out = []
        for r in rows:
            d = _summary(r)
            d["seq"] = r["saved_seq"]
            out.append(d)
        return out

    def close(self) -> None:
        """Flush pending writes, stop the writer and close the connection."""
        self._check_pid()
        if self._closed:
            return
        self._closed = True
        if self._queue is not None and self._thread is not None:
            try:
                self._queue.put(None, timeout=5.0)
            except queue.Full:  # pragma: no cover - writer wedged
                pass
            self._thread.join(timeout=10.0)
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _summary(row: sqlite3.Row) -> dict[str, Any]:
    kinds = [k for k in (row["issue_kinds"] or "").split(",") if k]
    return {
        "id": row["id"],
        "method": row["method"],
        "path": row["path"],
        "route": row["route"],
        "view": row["view"],
        "status": row["status"],
        "started_at": row["started_at"],
        "duration_ms": row["duration_ms"],
        "query_count": row["query_count"],
        "query_ms": row["query_ms"],
        "issue_count": row["issue_count"],
        "issue_kinds": kinds,
        "has_profile": bool(row["has_profile"]),
        "tags": json.loads(row["tags"]),
    }


# schema ----------------------------------------------------------------------
def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    try:
        return int(row[0]) if row else 1
    except ValueError:
        return 1


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database to :data:`SCHEMA_VERSION` in place (additive only).

    A v0.1 file (no ``meta`` row) is version 1. Version 2 adds the analytics and history
    tables and backfills them from the traces already stored, so the SQL / cache / external
    pages and the history have data at once. Version 3 adds the live-tail ``saved_seq``;
    version 4 the refactor-check tables (``check_suites``, ``check_runs``, ``check_results``).
    Runs in one ``BEGIN IMMEDIATE`` transaction and re-reads the version inside it, so
    concurrent processes migrate exactly once.
    """
    if _schema_version(conn) >= SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = _schema_version(conn)
        if version < 2:
            for statement in _SCHEMA_V2.split(";"):
                if statement.strip():
                    conn.execute(statement)
            _backfill(conn)
        if version < 3:
            # Insertion order for the live tail; existing traces keep their rowid order.
            if "saved_seq" not in {r[1] for r in conn.execute("PRAGMA table_info(traces)")}:
                conn.execute("ALTER TABLE traces ADD COLUMN saved_seq INTEGER")
            conn.execute("UPDATE traces SET saved_seq = rowid")
            conn.execute("CREATE INDEX IF NOT EXISTS traces_seq ON traces (saved_seq)")
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_seq', "
                "(SELECT CAST(COALESCE(MAX(saved_seq), 0) AS TEXT) FROM traces))"
            )
        if version < 4:
            # Refactor checks (CONTRACT.md §19.5): new tables only.
            for statement in SCHEMA_V4.split(";"):
                if statement.strip():
                    conn.execute(statement)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _next_seq(c: sqlite3.Connection, n: int) -> int:
    """Reserve ``n`` sequence numbers; returns the last one before them (call inside a
    ``BEGIN IMMEDIATE`` transaction)."""
    row = c.execute("SELECT value FROM meta WHERE key = 'last_seq'").fetchone()
    last = int(row[0]) if row else 0
    c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_seq', ?)", (str(last + n),))
    return last


def _backfill(conn: sqlite3.Connection, chunk: int = 500) -> None:
    cursor = conn.execute("SELECT body FROM traces ORDER BY started_at")
    while True:
        rows = cursor.fetchmany(chunk)
        if not rows:
            return
        traces = []
        for row in rows:
            try:
                traces.append(Trace.from_dict(json.loads(row[0])))
            except (ValueError, KeyError, TypeError):  # a corrupt row is skipped, not fatal
                continue
        _insert_derived(conn, derive(traces))


_ROLLUP_UPSERT = (
    "INSERT INTO rollups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (bucket, release, route, method) DO UPDATE SET "
    "count = count + excluded.count, errors = errors + excluded.errors, "
    "sum_ms = sum_ms + excluded.sum_ms, hist = excluded.hist, "
    "sum_queries = sum_queries + excluded.sum_queries, "
    "sum_query_ms = sum_query_ms + excluded.sum_query_ms, issues = issues + excluded.issues"
)
_RELEASE_UPSERT = (
    "INSERT INTO releases VALUES (?, ?, ?, ?) ON CONFLICT (release) DO UPDATE SET "
    "first_seen = min(first_seen, excluded.first_seen), "
    "last_seen = max(last_seen, excluded.last_seen), requests = requests + excluded.requests"
)
# Every SET expression reads the row's values from before the update.
_ISSUE_UPSERT = (
    "INSERT INTO issue_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (issue_id) DO UPDATE SET "
    "first_release = CASE WHEN excluded.first_seen < first_seen "
    "THEN excluded.first_release ELSE first_release END, "
    "first_seen = min(first_seen, excluded.first_seen), "
    "severity = CASE WHEN excluded.last_seen >= last_seen "
    "THEN excluded.severity ELSE severity END, "
    "title = CASE WHEN excluded.last_seen >= last_seen THEN excluded.title ELSE title END, "
    "last_release = CASE WHEN excluded.last_seen >= last_seen "
    "THEN excluded.last_release ELSE last_release END, "
    "last_seen = max(last_seen, excluded.last_seen), "
    "occurrences = occurrences + excluded.occurrences"
)
_ISSUE_BUCKET_UPSERT = (
    "INSERT INTO issue_rollups VALUES (?, ?, ?, ?) ON CONFLICT (bucket, release, issue_id) "
    "DO UPDATE SET occurrences = occurrences + excluded.occurrences"
)


def _insert_derived(c: sqlite3.Connection, d: Derived) -> None:
    """Write a :class:`Derived` inside the caller's transaction."""
    if d.sql:
        c.executemany("INSERT INTO trace_sql VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", d.sql)
    if d.ext:
        c.executemany("INSERT INTO trace_ext VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", d.ext)
    if d.cache:
        c.executemany("INSERT INTO trace_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", d.cache)
    if d.rollups:
        upserts = []
        for key, r in d.rollups.items():
            hist: Histogram = r[3]
            row = c.execute(
                "SELECT hist FROM rollups WHERE bucket = ? AND release = ? AND route = ? "
                "AND method = ?",
                key,
            ).fetchone()
            if row is not None:
                try:
                    hist = Histogram.from_bytes(row[0]).merge(hist)
                except ValueError:  # a corrupt blob is replaced; the counters still add up
                    pass
            upserts.append((*key, r[0], r[1], r[2], hist.to_bytes(), r[4], r[5], r[6]))
        c.executemany(_ROLLUP_UPSERT, upserts)
    if d.releases:
        c.executemany(_RELEASE_UPSERT, [(k, *v) for k, v in d.releases.items()])
    if d.issues:
        c.executemany(_ISSUE_UPSERT, [(k, *v) for k, v in d.issues.items()])
    if d.issue_buckets:
        c.executemany(_ISSUE_BUCKET_UPSERT, [(*k, n) for k, n in d.issue_buckets.items()])


# helpers ---------------------------------------------------------------------
def _like(text: str) -> str:
    """A case-insensitive ``LIKE`` pattern matching ``text`` anywhere (escape ``\\``)."""
    return "%" + text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _bursts(times: Any, threshold: int, span_s: float) -> int:
    """How many times ``threshold`` events fall within ``span_s`` seconds (sorted input).

    Each burst is counted once: after one is found, counting restarts past its window.
    """
    times = list(times)
    n, i, start = 0, 0, 0
    while i < len(times):
        while times[i] - times[start] > span_s:
            start += 1
        if i - start + 1 >= threshold:
            n += 1
            end = times[start] + span_s
            while i < len(times) and times[i] <= end:
                i += 1
            start = i
            continue
        i += 1
    return n


def _merge_blob(hist: Histogram, blob: bytes) -> None:
    try:
        hist.merge(Histogram.from_bytes(blob))
    except ValueError:  # a corrupt blob only loses its own percentiles
        logger.debug("Redline skipped a corrupt histogram blob")


def _period_dict(period: Period, stats: dict[tuple[str, str], PeriodStats]) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": period[0]}
    if period[0] == "release":
        out["release"] = period[1]
    else:
        out["start"], out["end"] = period[1], period[2]  # type: ignore[misc]
    out["requests"] = sum(s.count for s in stats.values())
    return out


def _index_entry(r: sqlite3.Row) -> IssueIndexEntry:
    return IssueIndexEntry(
        id=r["issue_id"],
        kind=r["kind"],
        severity=r["severity"],
        title=r["title"],
        route=r["route"],
        first_seen=r["first_seen"],
        last_seen=r["last_seen"],
        first_release=r["first_release"],
        last_release=r["last_release"],
        occurrences=r["occurrences"],
    )


def _step_statement(run_id: str, s: RunStep) -> tuple[str, tuple[Any, ...]]:
    return (
        "INSERT OR REPLACE INTO run_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            int(s.step),
            s.started_at,
            s.ended_at,
            s.target_rps,
            s.achieved_rps,
            s.sent,
            s.ok,
            s.errors,
            s.dropped,
            s.hist.to_bytes(),
            json.dumps(s.error_breakdown),
        ),
    )


def _run_step(r: sqlite3.Row) -> RunStep:
    try:
        hist = Histogram.from_bytes(r["hist"])
    except ValueError:
        hist = Histogram()
    return RunStep(
        step=r["step"],
        target_rps=r["target_rps"],
        started_at=r["started_at"],
        ended_at=r["ended_at"],
        achieved_rps=r["achieved_rps"],
        sent=r["sent"],
        ok=r["ok"],
        errors=r["errors"],
        dropped=r["dropped"],
        hist=hist,
        error_breakdown=json.loads(r["error_breakdown"] or "{}"),
    )


def _run(r: sqlite3.Row, steps: list[RunStep]) -> Run:
    return Run(
        id=r["id"],
        name=r["name"],
        status=r["status"],
        config=json.loads(r["config"] or "{}"),
        created_at=r["created_at"],
        finished_at=r["finished_at"],
        steps=steps,
        summary=json.loads(r["summary"] or "{}"),
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _add_sample(
    per_step: dict[int, list[Any]], step: int, count: int, total_ms: float, trace_id: str
) -> None:
    g = per_step.get(step)
    if g is None:
        g = per_step[step] = [0, 0.0, [], trace_id]
    g[0] += count
    g[1] += total_ms
    g[2].append(total_ms / count if count else 0.0)
    g[3] = trace_id
