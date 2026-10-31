"""Check that a trace store written by an older redline-core opens with this one.

Two phases, each run by a different interpreter:

    # 1. with an *old* redline-core installed (e.g. the latest release from PyPI)
    old-venv/bin/python scripts/check_store_upgrade.py write /tmp/upgrade/traces.db
    # 2. with *this* checkout installed
    python scripts/check_store_upgrade.py read /tmp/upgrade/traces.db

``write`` stores a small, deterministic set of traces (an N+1 burst, a slow request and a
tagged request) through the public ``SQLiteStore`` API and records what it wrote in a
sidecar JSON file. ``read`` opens the same file with the current code, which migrates the
schema in place, and verifies that every trace, its spans and its issues survived, that
the aggregate views see them, and that new writes still work. Exit status 0 = upgrade OK.

Only public, long-standing API is used in the write phase so the script runs against
every published 0.x release.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

N1_SQL = "SELECT id, name FROM customer WHERE id = ?"


def _manifest(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".manifest.json")


def _sql_span(index: int, sql: str, *, start_ms: float, duration_ms: float = 1.0) -> Any:
    from redline_core.model import Callsite, Span
    from redline_core.sql import fingerprint

    return Span(
        id=f"span-{index:04d}",
        parent_id=None,
        kind="sql",
        name=fingerprint(sql)[:120],
        start_ms=start_ms,
        duration_ms=duration_ms,
        callsite=Callsite(file="shop/views.py", line=42, function="order_detail"),
        data={
            "sql": sql,
            "params": [index],
            "fingerprint": fingerprint(sql),
            "db_alias": "default",
            "vendor": "sqlite",
            "rows": 1,
            "many": False,
            "explainable": True,
        },
    )


def _traces(now: float) -> list[Any]:
    from redline_core.detect import detect
    from redline_core.model import Trace

    def trace(trace_id: str, path: str, spans: list[Any], **extra: Any) -> Any:
        sql = [s for s in spans if s.kind == "sql"]
        t = Trace(
            id=trace_id,
            framework="other",
            method="GET",
            path=path,
            route=path,
            view="shop.views.order_detail",
            status=200,
            started_at=now,
            duration_ms=extra.pop("duration_ms", 12.0),
            query_count=len(sql),
            query_ms=sum(s.duration_ms for s in sql),
            spans=spans,
            **extra,
        )
        t.issues = detect(t)
        return t

    n1 = [_sql_span(i, N1_SQL, start_ms=float(i)) for i in range(8)]
    slow = [_sql_span(100, "SELECT * FROM report", start_ms=0.0, duration_ms=900.0)]
    tagged = [_sql_span(200, "SELECT 1", start_ms=0.0)]
    return [
        trace("upgrade-n1", "/orders/1/", n1),
        trace("upgrade-slow", "/reports/", slow, duration_ms=950.0),
        trace("upgrade-tagged", "/health/", tagged, tags={"suite": "upgrade"}),
    ]


def write(path: Path) -> int:
    import redline_core
    from redline_core.storage import SQLiteStore

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    traces = _traces(time.time() - 60)
    store = SQLiteStore(str(path), background=False)
    try:
        for t in traces:
            store.save(t)
    finally:
        store.close()
    manifest = {
        "written_by": getattr(redline_core, "__version__", "unknown"),
        "traces": {
            t.id: {
                "spans": len(t.spans),
                "issue_kinds": sorted({i.kind for i in t.issues}),
                "tags": dict(t.tags),
            }
            for t in traces
        },
    }
    _manifest(path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote {len(traces)} traces with redline-core {manifest['written_by']} to {path}")
    return 0


def read(path: Path) -> int:
    import redline_core
    from redline_core.storage import Filters, SQLiteStore
    from redline_core.storage.sqlite import SCHEMA_VERSION

    manifest = json.loads(_manifest(path).read_text(encoding="utf-8"))
    failures: list[str] = []
    store = SQLiteStore(str(path), background=False, max_rows=None, max_age_s=None)
    try:
        version = store._query("SELECT value FROM meta WHERE key = 'schema_version'")
        if [tuple(r) for r in version] != [(str(SCHEMA_VERSION),)]:
            failures.append(f"schema not migrated to {SCHEMA_VERSION}: {version!r}")
        if store.count() != len(manifest["traces"]):
            failures.append(f"expected {len(manifest['traces'])} traces, found {store.count()}")
        for trace_id, expected in manifest["traces"].items():
            got = store.get(trace_id)
            if got is None:
                failures.append(f"{trace_id}: missing after upgrade")
                continue
            if len(got.spans) != expected["spans"]:
                failures.append(f"{trace_id}: {len(got.spans)} spans, expected {expected['spans']}")
            kinds = sorted({i.kind for i in got.issues})
            if kinds != expected["issue_kinds"]:
                failures.append(f"{trace_id}: issues {kinds}, expected {expected['issue_kinds']}")
        if store.list(Filters(tag=("suite", "upgrade"))).total != 1:
            failures.append("tag filter does not find the tagged trace")
        if not store.issue_index():
            failures.append("issue index is empty after upgrade")
        before = store.count()
        fresh = _traces(time.time())[0]
        fresh.id = "upgrade-new-write"
        store.save(fresh)
        if store.count() != before + 1:
            failures.append("a new write after the upgrade was not stored")
    finally:
        store.close()
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(
        f"store written by redline-core {manifest['written_by']} opened with "
        f"{getattr(redline_core, '__version__', 'unknown')} (schema {SCHEMA_VERSION}): OK"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in {"write", "read"}:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(args[1])
    return write(path) if args[0] == "write" else read(path)


if __name__ == "__main__":
    sys.exit(main())
