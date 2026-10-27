"""Where a refactor check's results go, and where its server-side metrics come from.

* :class:`StoreCheckSink` writes a check run into the Redline store (``check_runs`` /
  ``check_results``, ``CONTRACT.md`` §19.5), so the UI shows it.
* :class:`StoreServerMetrics` reads back the app-side traces the run's calls produced
  (tagged from ``X-Redline-Check``, §19.2) and aggregates them per request.

Both talk to the store only; they are structurally compatible with the runner's
``CheckSink`` / ``ServerMetrics`` protocols (declared again here so this module does not
import the engine). Sink errors are logged and never stop a run.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import time
from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from ..redact import MASK, Redactor
from ..storage.derive import fingerprint_key

__all__ = [
    "CheckSink",
    "ServerMetrics",
    "StoreCheckSink",
    "StoreServerMetrics",
    "load_baseline",
    "redact_suite",
]

log = logging.getLogger("redline.check")

#: ``Bearer ${env:API_TOKEN}``, ``${env:COOKIE}``: placeholders, safe to keep.
_PLACEHOLDER = re.compile(r"(?:[A-Za-z][A-Za-z0-9_-]* )?\$\{env:[A-Za-z_][A-Za-z0-9_]*\}")


@runtime_checkable
class CheckSink(Protocol):
    def start(self, run: dict[str, Any]) -> None: ...

    def result(self, run_id: str, idx: int, name: str, data: dict[str, Any]) -> None: ...

    def finish(
        self, run_id: str, status: str, summary: dict[str, Any] | None, error: str | None
    ) -> None: ...


@runtime_checkable
class ServerMetrics(Protocol):
    def for_check(self, run_id: str, timeout_s: float = 5.0) -> dict[int, dict[str, Any]]: ...


def _plain(value: Any) -> Any:
    """Engine objects (``Suite``, ``Report`` parts…) → plain data."""
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return value


def is_placeholder(value: Any) -> bool:
    """Whether a header value is only an ``${env:NAME}`` placeholder (optionally after an
    auth scheme such as ``Bearer``)."""
    return isinstance(value, str) and _PLACEHOLDER.fullmatch(value.strip()) is not None


def _mask_headers(headers: Any, r: Redactor) -> Any:
    if not isinstance(headers, Mapping):
        return headers
    secret = r.headers({str(k): "x" for k in headers})
    return {
        str(k): v
        if is_placeholder(v) or (secret.get(str(k)) != MASK and not r.is_secret_key(str(k)))
        else MASK
        for k, v in headers.items()
    }


def redact_suite(suite: Any, redactor: Redactor | None = None) -> Any:
    """A suite dict safe to store with a run or serve: secret header values (by
    ``redact_headers`` / ``redact_keys``) are masked unless they are ``${env:…}``
    placeholders; secrets in ``base_url``, request paths and names are masked."""
    suite = _plain(suite)
    if not isinstance(suite, Mapping):
        return suite
    r = redactor or Redactor()
    out = dict(suite)
    if "headers" in out:
        out["headers"] = _mask_headers(out["headers"], r)
    if isinstance(out.get("base_url"), str):
        out["base_url"] = r.url(out["base_url"])
    requests = out.get("requests")
    if isinstance(requests, list):
        items = []
        for item in requests:
            if isinstance(item, Mapping):
                item = dict(item)
                if "headers" in item:
                    item["headers"] = _mask_headers(item["headers"], r)
                if isinstance(item.get("path"), str):
                    item["path"] = r.url(item["path"])
                if isinstance(item.get("name"), str):
                    item["name"] = r.text(item["name"])
            items.append(item)
        out["requests"] = items
    return out


def load_baseline(store: Any, baseline_id: str) -> dict[str, Any] | None:
    """A stored, finished baseline run as the §19.3 baseline JSON (what
    ``Baseline.from_dict`` reads), or ``None`` (``redline check compare --baseline-id``)."""
    fn = getattr(store, "check_baseline", None)
    if not callable(fn):
        return None
    return fn(baseline_id)


class StoreCheckSink:
    """Writes a check run into a store with the §19.5 check methods.

    ``start`` inserts (or completes) the ``running`` row with the redacted suite, ``result``
    stores one request's baseline data or comparison, ``finish`` sets the final status.
    """

    def __init__(self, store: Any, redactor: Redactor | None = None) -> None:
        self.store = store
        self.redactor = redactor or Redactor(getattr(store, "settings", None))

    def start(self, run: dict[str, Any]) -> None:
        try:
            data = dict(_plain(run))
            data["suite"] = redact_suite(data.get("suite"), self.redactor)
            data["status"] = "running"
            for key in ("git",):
                data[key] = _plain(data.get(key))
            self.store.create_check_run(data)
        except Exception:
            log.exception("Redline could not store the start of check run %s", run.get("id"))

    def result(self, run_id: str, idx: int, name: str, data: dict[str, Any]) -> None:
        try:
            plain = json.loads(json.dumps(_plain(data), default=_default))
            self.store.add_check_result(run_id, int(idx), str(name), plain)
        except Exception:
            log.exception("Redline could not store result %s of check run %s", idx, run_id)

    def finish(
        self, run_id: str, status: str, summary: dict[str, Any] | None, error: str | None
    ) -> None:
        if status not in ("done", "failed", "stopped"):
            status = "failed"
        try:
            plain = json.loads(json.dumps(_plain(summary), default=_default))
            self.store.finish_check_run(
                run_id, status, plain, self.redactor.text(error) if error else None
            )
        except Exception:
            log.exception("Redline could not store the end of check run %s", run_id)


def _default(value: Any) -> Any:
    plain = _plain(value)
    if plain is not value:
        return plain
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


class StoreServerMetrics:
    """Server-side metrics of a check run, from the traces the app stored for it.

    :meth:`for_check` flushes this process's writer, waits (up to ``timeout_s``) until the
    run's tagged traces stop arriving – or as many as the suite makes calls have arrived –
    then aggregates per ``check_req``: ``{req_index: {"duration_ms_median", "queries_median",
    "queries_max", "query_ms_median", "calls", "sql": {fingerprint: {"count", "max", "calls",
    "sql"}}, "issues": [{"kind", "fingerprint", "title", "id", "severity", "calls"}],
    "sample_trace_id"}}``. A request with no tagged trace is absent (``{}`` when none arrived:
    the app is not instrumented, uses another store, or dropped them).

    ``count`` of a SQL fingerprint is the median number of executions per call over the calls
    it ran in (``calls``), ``max`` the most in one call. Warm-up calls (``n=0``) are ignored.
    """

    #: Poll interval and how long the tagged-trace count must stay unchanged.
    POLL_S = 0.1
    SETTLE_S = 0.5

    def __init__(self, store: Any) -> None:
        self.store = store

    # waiting -----------------------------------------------------------------------
    def _expected_calls(self, run_id: str) -> int | None:
        get = getattr(self.store, "get_check_run", None)
        try:
            run = get(run_id) if callable(get) else None
        except Exception:
            return None
        suite = run.get("suite") if isinstance(run, dict) else None
        if not isinstance(suite, dict) or not isinstance(suite.get("requests"), list):
            return None
        try:
            per = int(suite["repeat"]) + int(suite["warmup"])
        except (KeyError, TypeError, ValueError):
            return None
        return per * len(suite["requests"])

    def _count(self, run_id: str) -> int:
        query = getattr(self.store, "_query", None)
        if callable(query):
            return int(
                query(
                    "SELECT COUNT(*) FROM trace_tags WHERE key = 'check_run_id' AND value = ?",
                    (run_id,),
                )[0][0]
            )
        from ..storage import Filters

        return int(self.store.list(Filters(tag=("check_run_id", run_id)), limit=1).total)

    def wait(self, run_id: str, timeout_s: float = 5.0) -> int:
        """Wait for the run's traces; returns how many are stored."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        flush = getattr(self.store, "flush", None)
        if callable(flush):
            try:
                flush(max(0.1, timeout_s))
            except Exception:  # pragma: no cover - defensive
                pass
        expected = self._expected_calls(run_id)
        last, since = -1, time.monotonic()
        while True:
            n = self._count(run_id)
            now = time.monotonic()
            if expected is not None and n >= expected:
                return n
            if n != last:
                last, since = n, now
            elif n > 0 and now - since >= self.SETTLE_S:
                return n
            if now >= deadline:
                return n
            time.sleep(min(self.POLL_S, max(0.0, deadline - now)))

    # aggregation ---------------------------------------------------------------------
    def for_check(self, run_id: str, timeout_s: float = 5.0) -> dict[int, dict[str, Any]]:
        try:
            if self.wait(run_id, timeout_s) == 0:
                return {}
            rows = self._traces(run_id)
        except Exception:
            log.exception("Redline could not read the traces of check run %s", run_id)
            return {}
        return _aggregate(rows)

    def _traces(self, run_id: str) -> list[dict[str, Any]]:
        """``[{id, req, duration_ms, query_count, query_ms, sql: [(fp, sql, count)],
        issues: [issue dict]}]`` of the run's valid tagged traces."""
        query = getattr(self.store, "_query", None)
        if callable(query):
            return self._traces_sqlite(query, run_id)
        return self._traces_generic(run_id)

    def _traces_sqlite(self, query: Any, run_id: str) -> list[dict[str, Any]]:
        rows = query(
            "SELECT t.id, t.duration_ms, t.query_count, t.query_ms, t.tags FROM trace_tags g "
            "JOIN traces t ON t.id = g.trace_id WHERE g.key = 'check_run_id' AND g.value = ? "
            "ORDER BY t.started_at",
            (run_id,),
        )
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            req = _req_of(json.loads(r["tags"] or "{}"))
            if req is None:
                continue
            out[r["id"]] = {
                "id": r["id"],
                "req": req,
                "duration_ms": float(r["duration_ms"]),
                "query_count": int(r["query_count"]),
                "query_ms": float(r["query_ms"]),
                "sql": [],
                "issues": [],
            }
        ids = list(out)
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for s in query(
                f"SELECT trace_id, fingerprint, sql, count FROM trace_sql WHERE trace_id IN "
                f"({marks})",
                chunk,
            ):
                out[s["trace_id"]]["sql"].append((s["fingerprint"], s["sql"], int(s["count"])))
            for s in query(f"SELECT trace_id, data FROM issues WHERE trace_id IN ({marks})", chunk):
                try:
                    out[s["trace_id"]]["issues"].append(json.loads(s["data"]))
                except ValueError:
                    continue
        return list(out.values())

    def _traces_generic(self, run_id: str) -> list[dict[str, Any]]:
        """Any store with ``list``/``get`` (slower: every trace is loaded)."""
        from ..storage import Filters

        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = self.store.list(Filters(tag=("check_run_id", run_id)), limit=500, cursor=cursor)
            for item in page.items:
                req = _req_of(item.get("tags") or {})
                trace = (
                    self.store.get(item["id"], include_profile=False) if req is not None else None
                )
                if trace is None:
                    continue
                sql: dict[str, list[Any]] = {}
                for span in trace.spans:
                    if span.kind != "sql":
                        continue
                    text = span.data.get("fingerprint") or span.name
                    entry = sql.setdefault(fingerprint_key(text), [text, 0])
                    entry[1] += 1
                out.append(
                    {
                        "id": trace.id,
                        "req": req,
                        "duration_ms": trace.duration_ms,
                        "query_count": trace.query_count,
                        "query_ms": trace.query_ms,
                        "sql": [(fp, v[0], v[1]) for fp, v in sql.items()],
                        "issues": [i.to_dict() for i in trace.issues],
                    }
                )
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                return out


def _req_of(tags: Mapping[str, Any]) -> int | None:
    try:
        req = int(tags.get("check_req"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    n = tags.get("check_n")
    try:
        if n is not None and int(n) <= 0:  # warm-up calls (n=0) are not measured
            return None
    except (TypeError, ValueError):
        return None
    return req if req >= 0 else None


def _num(value: float) -> float | int:
    value = round(float(value), 3)
    return int(value) if value.is_integer() else value


def _aggregate(traces: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_req: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for t in traces:
        by_req[t["req"]].append(t)
    out: dict[int, dict[str, Any]] = {}
    for req, items in sorted(by_req.items()):
        durations = [t["duration_ms"] for t in items]
        median = statistics.median(durations)
        sample = min(items, key=lambda t: (abs(t["duration_ms"] - median), t["id"]))
        per_fp: dict[str, list[int]] = defaultdict(list)
        texts: dict[str, str] = {}
        for t in items:
            for fp, text, count in t["sql"]:
                per_fp[fp].append(count)
                texts.setdefault(fp, text)
        sql = {
            fp: {
                "count": _num(statistics.median(counts)),
                "max": max(counts),
                "calls": len(counts),
                "sql": texts[fp],
            }
            for fp, counts in sorted(per_fp.items())
        }
        issues: dict[tuple[str, str], dict[str, Any]] = {}
        for t in items:
            seen: set[tuple[str, str]] = set()
            for issue in t["issues"]:
                kind = str(issue.get("kind"))
                raw = issue.get("fingerprint")
                fp = fingerprint_key(raw) if raw else str(issue.get("id"))
                key = (kind, fp)
                if key in seen:
                    continue
                seen.add(key)
                entry = issues.setdefault(
                    key,
                    {
                        "kind": kind,
                        "fingerprint": fp,
                        "title": issue.get("title"),
                        "id": issue.get("id"),
                        "severity": issue.get("severity"),
                        "calls": 0,
                    },
                )
                entry["calls"] += 1
        out[req] = {
            "calls": len(items),
            "duration_ms_median": _num(median),
            "queries_median": _num(statistics.median(t["query_count"] for t in items)),
            "queries_max": max(t["query_count"] for t in items),
            "query_ms_median": _num(statistics.median(t["query_ms"] for t in items)),
            "sql": sql,
            "issues": sorted(issues.values(), key=lambda i: (i["kind"], i["fingerprint"])),
            "sample_trace_id": sample["id"],
        }
    return out
