"""Read-only secondary trace browsing; mutations and aggregates use the primary store."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from .base import Page
from .sqlite import SQLiteStore, encode_cursor


class TraceReader:
    get = SQLiteStore.get
    get_profile = SQLiteStore.get_profile
    has_profile = SQLiteStore.has_profile
    count = SQLiteStore.count
    list = SQLiteStore.list
    issue_index = SQLiteStore.issue_index
    _where = staticmethod(SQLiteStore._where)

    def __init__(self, path):
        self.path = str(Path(path).resolve(strict=True))
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._conn = self._connect()

    def _connect(self):
        conn = sqlite3.connect(
            Path(self.path).as_uri() + "?mode=ro", uri=True, check_same_thread=False, timeout=5
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _query(self, sql, params=()):
        if self._pid != os.getpid():
            self._lock = threading.Lock()
            self._conn = self._connect()
            self._pid = os.getpid()
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self):
        self._conn.close()


class MultipleStores:
    """Merged browsing with secondary provenance; ambiguous IDs have no detail/profile."""

    def __init__(self, primary, paths):
        self.primary = primary
        self.readers = []
        try:
            for path in dict.fromkeys(str(Path(p).resolve(strict=True)) for p in paths):
                if Path(path).resolve() == Path(primary.path).resolve():
                    raise ValueError("extra_stores must differ from the primary store")
                self.readers.append(TraceReader(path))
        except BaseException:
            for reader in self.readers:
                reader.close()
            primary.close()
            raise

    def __getattr__(self, name):
        return getattr(self.primary, name)

    def _source(self, trace_id):
        """Resolve identity without loading bodies; duplicate IDs have no safe source."""
        found = None
        for index, store in enumerate((self.primary, *self.readers)):
            if store._query("SELECT 1 FROM traces WHERE id = ?", (trace_id,)):
                if found is not None:
                    return None
                found = index, store
        return found

    def get(self, trace_id, *, include_profile=True):
        source = self._source(trace_id)
        if source is None:
            return None
        index, store = source
        trace = store.get(trace_id, include_profile=include_profile)
        if trace is not None and index:
            trace.tags["secondary_store"] = str(index)
        return trace

    def get_profile(self, trace_id):
        source = self._source(trace_id)
        return source[1].get_profile(trace_id) if source is not None else None

    def has_profile(self, trace_id):
        source = self._source(trace_id)
        return source[1].has_profile(trace_id) if source is not None else False

    def delete(self, trace_id):
        source = self._source(trace_id)
        return self.primary.delete(trace_id) if source is not None and source[0] == 0 else 0

    def _mark_ambiguities(self, items):
        # Query the full stores, not just this filtered/cursor-limited page. A hidden
        # duplicate must not make a visible row look safe to inspect or profile.
        ids = tuple(dict.fromkeys(item["id"] for item in items))
        if not ids:
            return items
        seen, ambiguous = set(), set()
        for store in (self.primary, *self.readers):
            # list() is capped at 500; chunk the live-tail path as well.
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = store._query(f"SELECT id FROM traces WHERE id IN ({placeholders})", batch)
                matches = {row["id"] for row in rows}
                ambiguous.update(seen & matches)
                seen.update(matches)
        for item in items:
            if item["id"] in ambiguous:
                item["ambiguous_trace_id"] = True
                item["tags"] = {**item["tags"], "ambiguous_trace_id": "1"}
                item["has_profile"] = False
        return items

    def traces_since(self, *args, **kwargs):
        return self._mark_ambiguities(self.primary.traces_since(*args, **kwargs))

    def count(self):
        return sum(store.count() for store in (self.primary, *self.readers))

    def list(self, filters=None, limit=50, cursor=None):
        limit = max(1, min(int(limit), 500))
        pages = [
            store.list(filters, limit=limit, cursor=cursor)
            for store in (self.primary, *self.readers)
        ]
        items = []
        for index, page in enumerate(pages):
            for item in page.items:
                if index:
                    item["tags"] = {**item["tags"], "secondary_store": str(index)}
                items.append(item)
        items.sort(key=lambda i: (i["started_at"], i["id"]), reverse=True)
        more = len(items) > limit or any(p.next_cursor for p in pages)
        items = self._mark_ambiguities(items[:limit])
        cursor = encode_cursor(items[-1]["started_at"], items[-1]["id"]) if more and items else None
        return Page(items=items, next_cursor=cursor, total=sum(p.total for p in pages))

    def close(self):
        for reader in self.readers:
            reader.close()
        self.primary.close()
