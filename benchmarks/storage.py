"""Measure what Redline stores: bytes per trace on disk, writer throughput, span memory.

    uv run python benchmarks/storage.py            # prints a markdown report
    uv run python benchmarks/storage.py --quick    # fewer traces (smoke test)

Framework-free: traces are recorded through the core recorder exactly as the adapters do
(``start_trace`` -> ``record_query`` per query, with callsite capture -> ``finish_trace``),
with browser-like request headers and a small JSON response, and stored in a fresh
``SQLiteStore`` file. Every query runs from one line in a loop, so the 20- and 200-query
shapes carry an N+1 issue (with its fix suggestion) like a real problem endpoint.

Reported:

* **bytes per trace** - file size (``.db`` after a WAL checkpoint) / traces stored;
* **profile size** - one pyinstrument flamegraph of a ~20 ms request;
* **writer throughput** - traces/s the background writer stores (the kept-trace rate above
  which ``storage.dropped`` starts to grow);
* **memory per in-flight span** - ``tracemalloc`` growth while one request records queries.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import sys
import tempfile
import time
import tracemalloc

from redline_core import Recorder, Settings, record_query
from redline_core.storage import SQLiteStore

HERE = os.path.dirname(os.path.abspath(__file__))
HEADERS = {
    "host": "localhost:8000",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-GB,en;q=0.9",
    "accept-encoding": "gzip, deflate, br",
    "cookie": "sessionid=abc123; csrftoken=xyz",
    "referer": "http://localhost:8000/orders/",
}
SQL = (
    'SELECT "shop_orderitem"."id", "shop_orderitem"."order_id", "shop_orderitem"."product_id",'
    ' "shop_orderitem"."qty" FROM "shop_orderitem" WHERE "shop_orderitem"."order_id" = %s'
)


def response_body(n: int) -> bytes:
    items = [{"id": i, "customer": f"customer {i}", "total": "12.50"} for i in range(max(n, 3))]
    return json.dumps({"orders": items[:20]}).encode()


def record_one(rec: Recorder, n_queries: int, *, profile: bool = False):
    builder = rec.start_trace(
        framework="other",
        method="GET",
        path="/orders/",
        route="/orders/",
        view="shop.views.order_list",
        headers=HEADERS,
        query="page=2",
        profile_requested=profile,
    )
    for i in range(n_queries):
        t0 = time.perf_counter()
        record_query(SQL, [i], start=t0, duration=0.0002, vendor="sqlite", rows=2)  # loop line
    if profile:
        end = time.perf_counter() + 0.02
        x = 0
        while time.perf_counter() < end:  # ~20 ms of Python work to sample
            x += sum(range(200))
    body = response_body(n_queries)
    return rec.finish_trace(
        builder,
        status=200,
        headers=[("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
        body=body,
    )


def file_bytes(path: str) -> int:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return sum(os.path.getsize(p) for p in (path, path + "-wal") if os.path.exists(p))


def size_case(n_queries: int, traces: int, *, profile: bool = False) -> dict:
    tmp = tempfile.mkdtemp(prefix="redline-size-")
    path = os.path.join(tmp, "redline.db")
    store = SQLiteStore(path, background=False, max_rows=None, max_age_s=None)
    rec = Recorder(Settings.from_mapping({"project_root": HERE, "too_many_queries": 10_000}), store)
    empty = file_bytes(path)
    trace = None
    for _ in range(traces):
        trace = record_one(rec, n_queries, profile=profile)
    rows = store.count()
    profile_bytes = None
    if profile:
        profile_bytes = store._query("SELECT avg(length(data)) FROM profiles")[0][0]
    store.close()
    total = file_bytes(path) - empty
    return {
        "queries": n_queries,
        "traces": rows,
        "bytes_per_trace": total / rows,
        "issues": [i.kind for i in trace.issues] if trace else [],
        "profile_bytes": profile_bytes,
    }


def throughput(n_queries: int, traces: int) -> float:
    """Traces/s the background writer stores (the save side is pre-recorded)."""
    tmp = tempfile.mkdtemp(prefix="redline-writer-")
    sink = SQLiteStore(os.path.join(tmp, "x.db"), background=False)
    prepared = []

    class Capture:
        def save(self, trace):
            prepared.append(trace)

    rec = Recorder(
        Settings.from_mapping({"project_root": HERE, "too_many_queries": 10_000}),
        Capture(),  # type: ignore[arg-type]
    )
    for _ in range(traces):
        record_one(rec, n_queries)
    sink.close()
    store = SQLiteStore(
        os.path.join(tmp, "redline.db"), queue_size=traces + 10, max_rows=None, max_age_s=None
    )
    store.save(prepared[0])  # start the writer outside the timed part
    store.flush(30)
    t = time.perf_counter()
    for trace in prepared[1:]:
        store.save(trace)
    store.flush(300)
    elapsed = time.perf_counter() - t
    store.close()
    return (len(prepared) - 1) / elapsed


def span_memory(n_queries: int) -> float:
    rec = Recorder(Settings.from_mapping({"project_root": HERE, "max_spans_per_trace": 0}))
    builder = rec.start_trace(method="GET", path="/m/")
    record_query(SQL, [0], start=time.perf_counter(), duration=0.0001, vendor="sqlite")
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    for i in range(n_queries):
        record_query(SQL, [i], start=time.perf_counter(), duration=0.0001, vendor="sqlite")
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    grown = sum(s.size_diff for s in after.compare_to(before, "filename"))
    rec.finish_trace(builder, status=200)
    return grown / n_queries


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    n = 200 if args.quick else 2_000

    sizes = [size_case(q, n if q < 200 else n // 4) for q in (0, 20, 200)]
    prof = size_case(20, 20 if args.quick else 100, profile=True)
    rates = {q: throughput(q, n // 2) for q in (0, 20, 200)}
    mem = span_memory(2_000)

    print("## Storage size and writer throughput\n")
    print(f"- Date: {time.strftime('%Y-%m-%d')}")
    print(
        f"- Python: {platform.python_version()} ({platform.python_implementation()}) on "
        f"{platform.system()} {platform.machine()}"
    )
    print("- Reproduce with `uv run python benchmarks/storage.py` (about a minute).")
    print("- Shape: browser-like headers, `?page=2`, a JSON response of about 1 KB, every")
    print("  query from one loop line (so 20 and 200 queries carry an N+1 issue with its")
    print("  fix suggestion). Request bodies off (a GET). Retention off while measuring.\n")
    print("| queries / trace | traces | bytes / trace on disk | issues |")
    print("|---|---|---|---|")
    for r in sizes:
        print(
            f"| {r['queries']} | {r['traces']} | {r['bytes_per_trace'] / 1024:.1f} KiB | "
            f"{', '.join(r['issues']) or 'none'} |"
        )
    print(
        f"\nProfile (pyinstrument, ~20 ms request): {prof['profile_bytes'] / 1024:.1f} KiB "
        f"of speedscope JSON per profiled trace (whole profiled trace: "
        f"{prof['bytes_per_trace'] / 1024:.1f} KiB).\n"
    )
    print("| queries / trace | writer throughput (traces/s) |")
    print("|---|---|")
    for q, rate in rates.items():
        print(f"| {q} | {rate:,.0f} |")
    print(
        f"\nMemory per in-flight SQL span (tracemalloc, 2000 queries in one request): "
        f"{mem:,.0f} bytes; the default `max_spans_per_trace = 2000` caps a request at "
        f"about {mem * 2000 / 1024 / 1024:.1f} MiB of spans."
    )
    sys.stdout.flush()


if __name__ == "__main__":
    main()
