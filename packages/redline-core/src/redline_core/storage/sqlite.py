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
"""

from __future__ import annotations

import builtins
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

from ..model import Issue, Trace
from .base import (
    MAX_BUCKETS,
    EndpointStats,
    Filters,
    IssueGroup,
    Page,
    decode_cursor,
    encode_cursor,
    percentile,
)

__all__ = ["SQLiteStore"]

logger = logging.getLogger("redline")

_SCHEMA = """
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
"""

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


class SQLiteStore:
    """SQLite-backed :class:`~redline_core.storage.Store`.

    :param path: database file; parent directories are created. ``":memory:"`` works.
    :param background: write on a daemon thread (default). ``False`` writes inline,
        which tests and short scripts find convenient.
    :param queue_size: bound of the write queue; overflow is dropped and counted.
    :param max_rows, max_age_s: retention applied by :meth:`prune`, which the writer runs
        every ``prune_every`` saved traces.
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
    ) -> None:
        path = os.fspath(path)
        for name, value in (("max_rows", max_rows), ("max_age_s", max_age_s)):
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative (use 0 or None to disable it).")
        self.path = path
        #: Retention rules; ``0``/``None`` disables a rule.
        self.max_rows = max_rows or None
        self.max_age_s = max_age_s or None
        self.prune_every = max(1, prune_every)
        self.batch_size = max(1, batch_size)
        self.background = background
        self.queue_size = max(1, queue_size)
        self.dropped = 0
        self.written = 0
        self._since_prune = 0
        self._closed = False
        if path != ":memory:" and not path.startswith("file:"):
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
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
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
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
            logger.exception("Redline could not reopen its store after fork")
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
        return cls(
            settings.storage_path,
            queue_size=settings.writer_queue_size,
            max_rows=settings.max_rows,
            max_age_s=settings.max_age_s,
            **kwargs,
        )

    # writing ---------------------------------------------------------------
    def save(self, trace: Trace) -> None:
        """Enqueue ``trace`` (never blocks); dropped and counted when the queue is full."""
        self._check_pid()
        if self._closed:
            self.dropped += 1
            return
        if not self.background:
            self._write([trace])
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
            markers: list[threading.Event] = []
            stop = False
            while True:
                if item is None:
                    stop = True
                elif isinstance(item, threading.Event):
                    markers.append(item)
                else:
                    batch.append(item)
                if stop or len(batch) >= self.batch_size:
                    break
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
            if batch:
                try:
                    self._write(batch)
                except Exception:  # keep the writer alive whatever happens
                    logger.exception("Redline writer failed to store %d trace(s)", len(batch))
            for m in markers:
                m.set()
            if stop:
                return

    def _write(self, traces: list[Trace]) -> None:
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
        with self._lock:
            c = self._conn
            c.execute("BEGIN")
            try:
                ids = [(t.id,) for t in traces]
                c.executemany("DELETE FROM trace_tags WHERE trace_id = ?", ids)
                c.executemany("DELETE FROM issues WHERE trace_id = ?", ids)
                c.executemany(
                    "INSERT OR REPLACE INTO traces VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                c.executemany("INSERT INTO trace_tags VALUES (?, ?, ?)", tags)
                c.executemany("INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?)", issues)
                c.executemany("INSERT OR REPLACE INTO profiles VALUES (?, ?)", profiles)
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
            like = (
                "%"
                + f.q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%"
            )
            clauses.append(
                "(lower(t.path) LIKE ? ESCAPE '\\' OR lower(coalesce(t.route, '')) LIKE ? "
                "ESCAPE '\\' OR lower(coalesce(t.view, '')) LIKE ? ESCAPE '\\')"
            )
            params += [like, like, like]
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
                    "routes": [],
                }
            g["n"] += 1
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
            c.execute("BEGIN")
            try:
                for table, col in (
                    ("profiles", "trace_id"),
                    ("trace_tags", "trace_id"),
                    ("issues", "trace_id"),
                ):
                    c.executemany(f"DELETE FROM {table} WHERE {col} = ?", params)
                c.executemany("DELETE FROM traces WHERE id = ?", params)
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return len(ids)

    def delete(self, trace_id: str) -> int:
        exists = self._query("SELECT id FROM traces WHERE id = ?", (trace_id,))
        return self._delete_ids([r["id"] for r in exists])

    def clear(self) -> int:
        self._check_pid()
        with self._lock:
            c = self._conn
            n = int(c.execute("SELECT COUNT(*) FROM traces").fetchone()[0])
            c.execute("BEGIN")
            for table in ("profiles", "trace_tags", "issues", "traces"):
                c.execute(f"DELETE FROM {table}")
            c.execute("COMMIT")
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
        return self._delete_ids(doomed)

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
