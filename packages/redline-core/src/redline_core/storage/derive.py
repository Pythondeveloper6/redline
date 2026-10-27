"""Turn stored traces into the rows of the v0.2 analytics tables (``CONTRACT.md`` §11).

:func:`derive` reads a batch of traces once and returns everything the writer inserts next
to them: per-trace SQL / outbound HTTP / cache aggregates and the batch's contribution to
the long-lived history tables (hourly rollups, releases, the issue index). It is pure
Python with no database access, so the writer computes it outside its lock.

Patterns (``url_pattern``, ``key_pattern``) replace volatile parts with ``*``: UUIDs, hex
runs of 8+ characters and digit runs that are not glued to a preceding letter (``v2`` and
``utf8`` stay, ``/orders/42`` becomes ``/orders/*``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .. import sql as sqlmod
from ..histogram import Histogram
from ..model import Trace
from ..sampling import is_error

__all__ = [
    "HOUR",
    "UNMATCHED",
    "Derived",
    "derive",
    "fingerprint_key",
    "is_failed_status",
    "pattern",
]

HOUR = 3_600
#: Rollup / issue-index route of requests no route matched (404s, scanners), so the
#: long-lived history does not grow a row per random path.
UNMATCHED = "(unmatched)"

_UUID = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)
_HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8,}(?![0-9A-Za-z])")
_DIGITS = re.compile(r"(?<![A-Za-z0-9])\d+")

#: Cache operations by effect (Django cache API and redis-py command names, lower case).
_GET_OPS = frozenset(
    {"get", "get_many", "get_or_set", "has_key", "mget", "hget", "hmget", "hgetall",
     "exists", "getex", "getdel", "aget", "aget_many", "ahas_key", "aget_or_set"}
)  # fmt: skip
_SET_OPS = frozenset(
    {"set", "add", "set_many", "setex", "psetex", "setnx", "mset", "hset", "hmset",
     "incr", "decr", "incrby", "decrby", "aset", "aadd", "aset_many", "aincr", "adecr"}
)  # fmt: skip
_DELETE_OPS = frozenset(
    {"delete", "delete_many", "del", "unlink", "hdel", "clear", "adelete", "adelete_many",
     "aclear"}
)  # fmt: skip

_FP_KEYS: dict[str, str] = {}


def pattern(text: str) -> str:
    """``text`` with UUIDs, long hex runs and free-standing digit runs replaced by ``*``."""
    text = _UUID.sub("*", text)
    text = _HEX.sub("*", text)
    return _DIGITS.sub("*", text)


def fingerprint_key(fp: str) -> str:
    """The 16-hex key of a normalised statement (same as ``sql.fingerprint_hash(raw)``)."""
    key = _FP_KEYS.get(fp)
    if key is None:
        if len(_FP_KEYS) > 4_096:
            _FP_KEYS.clear()
        key = _FP_KEYS[fp] = hashlib.sha1(fp.encode("utf-8")).hexdigest()[:16]
    return key


def is_failed_status(status: Any) -> bool:
    """An outbound call failed: no response, a 5xx or a 429 (as in ``retry_storm``)."""
    return not isinstance(status, int) or status >= 500 or status == 429


def url_parts(url: str, name: str = "") -> tuple[str, str]:
    """``(host, url_pattern)`` for an outbound URL; the query string is dropped."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        scheme = parts.scheme
        path = parts.path
    except ValueError:
        host, scheme, path = "", "", ""
    if not host:  # no usable URL: fall back to the span name "GET host/path"
        target = name.partition(" ")[2] or url
        host, _, rest = target.partition("/")
        return host, pattern(f"{host}/{rest.split('?')[0]}" if host else url.split("?")[0])
    return host, f"{scheme}://{host}{pattern(path)}" if scheme else f"{host}{pattern(path)}"


@dataclass
class Derived:
    """Rows for one batch of traces, ready for ``executemany``."""

    sql: list[tuple[Any, ...]] = field(default_factory=list)
    ext: list[tuple[Any, ...]] = field(default_factory=list)
    cache: list[tuple[Any, ...]] = field(default_factory=list)
    #: ``(bucket, release, route, method) -> [count, errors, sum_ms, hist, sum_queries,
    #: sum_query_ms, issues]``
    rollups: dict[tuple[int, str, str, str], list[Any]] = field(default_factory=dict)
    #: ``release -> [first_seen, last_seen, requests]``
    releases: dict[str, list[Any]] = field(default_factory=dict)
    #: ``issue_id -> [kind, severity, title, route, first_seen, last_seen, first_release,
    #: last_release, occurrences]``
    issues: dict[str, list[Any]] = field(default_factory=dict)
    #: ``(bucket, release, issue_id) -> occurrences``
    issue_buckets: dict[tuple[int, str, str], int] = field(default_factory=dict)


def derive(traces: list[Trace], *, history: bool = True, out: Derived | None = None) -> Derived:
    """Aggregate ``traces`` into a :class:`Derived` (``history=False``: trace tables only)."""
    d = out or Derived()
    for t in traces:
        route = t.route or t.path
        _trace_rows(t, route, d)
        if history:
            _history(t, t.route or UNMATCHED, d)
    return d


def _trace_rows(t: Trace, route: str, d: Derived) -> None:
    sql: dict[str, list[Any]] = {}
    ext: dict[tuple[str, str, str], list[Any]] = {}
    cache: dict[tuple[str, str], list[int]] = {}
    for s in t.spans:
        kind = s.kind
        if kind == "sql":
            data = s.data
            fp = data.get("fingerprint") or sqlmod.fingerprint(str(data.get("sql", "")))
            dur = s.duration_ms
            rows = data.get("rows")
            g = sql.get(fp)
            if g is None:
                # count, total, max, rows, rows_n, vendor, alias
                g = sql[fp] = [0, 0.0, 0.0, 0, 0, data.get("vendor"), data.get("db_alias")]
            g[0] += 1
            g[1] += dur
            if dur > g[2]:
                g[2] = dur
            if isinstance(rows, int) and not isinstance(rows, bool):
                g[3] += rows
                g[4] += 1
        elif kind == "http_out":
            data = s.data
            method = str(data.get("method") or s.name.partition(" ")[0] or "GET").upper()
            host, url = url_parts(str(data.get("url") or ""), s.name)
            key = (method, host, url)
            e = ext.get(key)
            if e is None:
                e = ext[key] = [0, 0.0, 0.0, 0]
            dur = s.duration_ms
            e[0] += 1
            e[1] += dur
            if dur > e[2]:
                e[2] = dur
            if is_failed_status(data.get("status")):
                e[3] += 1
        elif kind == "cache":
            data = s.data
            op = str(data.get("op") or "").lower()
            key = data.get("key")
            if not key or not op:
                continue
            ck = (str(data.get("backend") or "default"), pattern(str(key))[:200])
            c = cache.get(ck)
            if c is None:
                c = cache[ck] = [0, 0, 0, 0, 0]  # gets, hits, misses, sets, deletes
            if op in _GET_OPS:
                c[0] += 1
                hit = data.get("hit")
                if hit is True:
                    c[1] += 1
                elif hit is False:
                    c[2] += 1
            elif op in _SET_OPS:
                c[3] += 1
            elif op in _DELETE_OPS:
                c[4] += 1
    tid, at, method = t.id, t.started_at, t.method
    for fp, g in sql.items():
        d.sql.append(
            (tid, at, route, method, fingerprint_key(fp), fp, g[0], g[1], g[2],
             g[3] if g[4] else None, g[4], g[5] or "other", g[6] or "default")
        )  # fmt: skip
    for (m, host, url), e in ext.items():
        d.ext.append((tid, at, m, host, url, e[0], e[1], e[2], e[3]))
    for (backend, kp), c in cache.items():
        d.cache.append((tid, at, backend, kp, c[0], c[1], c[2], c[3], c[4]))


def _history(t: Trace, route: str, d: Derived) -> None:
    at = t.started_at
    bucket = int(at // HOUR) * HOUR
    release = t.release or ""
    key = (bucket, release, route, t.method)
    r = d.rollups.get(key)
    if r is None:
        r = d.rollups[key] = [0, 0, 0.0, Histogram(), 0, 0.0, 0]
    r[0] += 1
    if is_error(t.status, t.error is not None):
        r[1] += 1
    r[2] += t.duration_ms
    r[3].record(t.duration_ms)
    r[4] += t.query_count
    r[5] += t.query_ms
    r[6] += len(t.issues)
    if release:
        rel = d.releases.get(release)
        if rel is None:
            d.releases[release] = [at, at, 1]
        else:
            rel[0] = min(rel[0], at)
            rel[1] = max(rel[1], at)
            rel[2] += 1
    for issue in t.issues:
        entry = d.issues.get(issue.id)
        rel_or_none = t.release
        if entry is None:
            d.issues[issue.id] = [issue.kind, issue.severity, issue.title, route, at, at,
                                  rel_or_none, rel_or_none, 1]  # fmt: skip
        else:
            if at < entry[4]:
                entry[4], entry[6] = at, rel_or_none
            if at >= entry[5]:
                entry[5], entry[7] = at, rel_or_none
                entry[1], entry[2] = issue.severity, issue.title
            entry[8] += 1
        ib = (bucket, release, issue.id)
        d.issue_buckets[ib] = d.issue_buckets.get(ib, 0) + 1
