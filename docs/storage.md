# Storage

[← Index](index.md)

Redline keeps traces in a **separate SQLite file** of its own, written by a background
thread. This page covers the design, the schema, retention, the failure modes, and how to
look inside.

## Why a separate file

Each choice fixes something that makes in-database profilers painful.

| Choice | Reason |
|---|---|
| **Its own file**, never your application database | Profiling data cannot bloat your database, you need no migrations, and Redline's writes can never be recorded as your queries (it is not a Django connection nor one of your SQLAlchemy engines) |
| **WAL mode** | The UI can read while the writer writes |
| **A bounded queue and a writer thread** | The request path only does a `put_nowait`; disk I/O happens elsewhere |
| **Stdlib `sqlite3`** | `redline-core` has no runtime dependencies |

## Where the file is

| | Default |
|---|---|
| Django | `<BASE_DIR>/.redline/redline.db` |
| FastAPI, Flask, Litestar | `.redline/redline.db`, relative to the **working directory** at start |
| Any | the `storage_path` setting. `":memory:"` works (nothing persists) |

Parent directories are created. Add `.redline/` to `.gitignore`. The `-wal` and `-shm`
files next to it belong to the database while it is open; do not delete them while the app
runs.

## The schema

Created on first open with `CREATE TABLE IF NOT EXISTS`.

| Table | Holds | Notes |
|---|---|---|
| `traces` | one row per trace: id, started_at, framework, method, path, route, view, status, duration_ms, query_count, query_ms, issue_count, issue_kinds, has_profile, tags, and `body` | `body` is the full trace as JSON (spans, issues, redacted request and response, **without** the profile). Indexed by start time and by `(route, method)` |
| `profiles` | one row per profiled trace: the speedscope JSON | Kept apart so lists never load it |
| `trace_tags` | `(trace_id, key, value)` | makes `tag=key:value` filters fast |
| `issues` | one row per issue per trace, with the issue as JSON | Indexed by issue id and time; this is what the Issues page aggregates |

### Schema version 3 (0.2)

Version 0.2 adds a `meta(key, value)` table holding `schema_version` (`3` in 0.2, `4` since 0.3; see below) and a set of
**derived tables** that are written in the same transaction as the trace, from the same
batch:

| Table | Holds | Lives |
|---|---|---|
| `trace_sql` | one row per (trace, SQL fingerprint): the normalised statement, execution count, total and max time, rows, vendor, db alias | with its trace |
| `trace_ext` | one row per (trace, outbound URL pattern): count, total and max time, errors | with its trace |
| `trace_cache` | one row per (trace, cache key pattern): gets, hits, misses, sets, deletes | with its trace |
| `rollups` | hourly per release, route and method: count, errors, summed duration, a latency **histogram**, summed queries and query time, issue count | `history_retention_days` |
| `issue_rollups` | hourly per release and issue: occurrences (for period comparison) | `history_retention_days` |
| `releases` | release name, first and last seen, request count | `history_retention_days` |
| `issue_index` | every issue ever seen: kind, severity, title, route, first and last seen, first and last release, occurrences | `history_retention_days` |
| `runs`, `run_steps` | [load-test](load-testing.md) runs and their per-step results and histograms | `history_retention_days` |

Version 3 also adds `saved_seq` to `traces`: an insertion-order sequence number (indexed,
`meta` keeps the high-water mark). The live stream uses it to resume exactly after the last
event a client saw (`after=` or `Last-Event-ID`), because traces are saved when they
*finish*, not when they start.

In the rollups and the issue index a request that matched no route (a 404, a scanner
probing random paths) is filed under the single route key `(unmatched)`, so the long-lived
history does not grow a row per random path.

The first three power the [Database](database.md), [Cache](cache-efficiency.md) and
[External calls](external-calls.md) pages and are pruned **with their trace**. The rest
power [History](history.md) and the Load page and outlive the traces.

A re-saved trace id replaces its trace-scoped rows and is **not** counted again in rollups,
releases or the issue index. History reflects **kept** traces, so [sampling](production.md#sampling)
applies to it.

### Schema version 4 (0.3)

Version 0.3 adds the [refactor check](refactor-check.md) tables. They are new tables only; no
existing table changes.

| Table | Holds | Lives |
|---|---|---|
| `check_suites` | a saved suite: id, name, the suite as JSON (secret header values redacted, `${env:NAME}` placeholders kept), created and updated time | until you delete the suite (which deletes its runs) |
| `check_runs` | one row per baseline or compare run: kind, status (`running`, `done`, `failed`, `stopped`), label, commit sha and branch, the baseline it was compared with, a summary, the redacted suite snapshot, base URL, release, progress and error | the 50 most recent per suite |
| `check_results` | one row per request of a run, `(run_id, idx)`: the baseline data or the comparison result as JSON | with its run |

Retention is by count, not age: whenever a run is created or a baseline is imported, each
suite keeps its **50 newest runs** (runs without a suite form one group). A run that is still
running is never removed, and neither is a baseline that a kept compare run refers to.
**Clear all** (`DELETE api/traces`) and `redline_clear` do not touch these tables. A `running`
row whose background process died (checked when runs are listed, after 10 seconds) becomes
`failed` with the error `the check process exited unexpectedly`.

The tables are written synchronously, outside the trace writer's batches, because a check run
is a handful of writes and the report must be readable the moment the run ends.

### Migration

Migrations are additive and run when the store connects. A 0.1 file (no `schema_version`
row) a version 2 file or a version 3 file is migrated **in place**, inside one `BEGIN IMMEDIATE` transaction that re-checks the
version once it holds the lock, so several workers starting at once migrate it exactly once.
The new tables are created and **backfilled from the traces already stored**. Traces
written by 0.1 still load: every new field has a default.

There is no downgrade. If you may need to roll back to 0.1, copy `.redline/redline.db`
first. The data is a cache of recent requests, so deleting the file is always an
acceptable fix for a store that will not open.

## The writer thread

`SQLiteStore.save(trace)` puts the trace on a bounded queue (`writer_queue_size`, default
1000) and returns. A daemon thread named `redline-writer`, started on the first `save` in
each process:

1. takes one item, then drains up to **50** more that are already waiting (a batch);
2. writes the batch in **one transaction**, opened with `BEGIN IMMEDIATE` (as are pruning
   and migrations), so concurrent worker processes take the write lock up front and wait
   instead of failing with a busy error halfway through;
3. every 200 saved traces, runs [retention](#retention);
4. survives errors: a failed batch is logged on the `redline` logger and the thread
   carries on.

### Overload

If the queue is full, `save` does not wait: the trace is **dropped and counted**. The
count is `store.dropped`, exposed as `storage.dropped` in `GET api/meta`. It means the
writer cannot keep up with the rate of kept traces. Lower `sample_rate`, turn off
body capture, or raise the queue bound (which only postpones the problem and uses memory).
`store.written` counts what was stored.

### Flushing and shutdown

`store.flush(timeout)` blocks until everything enqueued so far is written (it returns
`False` on timeout). `store.close()` flushes, stops the thread and closes the connection.
Both adapters call them at shutdown (FastAPI: the lifespan wrapper; Django: an `atexit`
hook). A hard kill loses whatever was still queued.

### Concurrency

One connection is shared behind a lock. SQLite serialises writers anyway, and the UI's
reads are small, but note:

- UI reads and the writer take turns. A long aggregate (a 7-day Endpoints page over many
  thousands of traces) delays the writer for its duration.
- Several **processes** can share the file (WAL, 10-second busy timeout), but see
  [Production: process models](production.md#process-models) for the caveats.
- After `fork()` (gunicorn `--preload`, uWSGI without `lazy-apps`) the store opens a
  new connection, queue and writer thread in the child; the parent's connection is never
  touched there. See [Production: process models](production.md#process-models).
- `SQLiteStore(background=False)` writes inline on the calling thread. That is for tests
  and short scripts, **not** for serving requests.

## Retention

Two rules, applied together.

| Rule | Setting | Behaviour |
|---|---|---|
| Age | `max_age_days` (default 7) | delete traces whose start is older than this. `0` or `None` disables the age rule |
| Count | `max_rows` (default 10,000) | keep the newest N, delete the rest. `0` or `None` disables the row cap |

Zero means "no limit" for both rules, and a negative value is an error. Pruning never
deletes everything because of a zero; that is what **Clear all** is for.

Applied by the writer every 200 saved traces, by `manage.py redline_prune` (Django) on
demand, and when you call `store.prune(max_age_s=..., max_rows=...)`. Pruning removes the
trace and its profile, tags and issue rows together. **Clear all** (`DELETE api/traces`)
and `manage.py redline_clear` remove everything.

Because retention is checked per 200 writes, the store can briefly exceed `max_rows` by up
to roughly that many.

### History tables

The [history](history.md) tables have their own, longer clock.

| Setting | Default | Covers |
|---|---|---|
| `history_retention_days` | `90` | `rollups`, `issue_rollups`, `releases`, `issue_index`, and load `runs` with their steps. `0` or `None` keeps them forever |

When pruning runs it also calls `prune_history()`: rollup hours that ended before the
cutoff are deleted, as are releases and issue-index entries **last seen** before it, and
finished runs created before it. A run that is still `running` is never deleted. The trace
summary tables (`trace_sql`, `trace_ext`, `trace_cache`) follow their traces.

**Clear all** and `redline_clear` delete traces and **keep history**, so a clean-up does not
erase your release comparisons. `store.clear(history=True)` wipes history as well.

!!! note "Retention shapes the Issues and Endpoints pages"

    Both pages only know about traces that still exist. A 7-day window with
    `max_age_days = 2` shows two days. A `max_rows` that holds an hour of traffic makes a
    `24h` window show an hour.

## Size

Measured with `benchmarks/storage.py` on 2026-10-26 (Apple M2 Pro, Python 3.13.15), for a
`GET` with browser-like headers and a JSON response of about 1 KB, every query issued
from one loop line (so the 20- and 200-query traces carry an N+1 issue and its fix
suggestion). Bytes are the `.db` file after a WAL checkpoint, divided by the traces stored:

| Trace | On disk per trace |
|---|---|
| no queries | **1.6 KiB** |
| 20 queries | **31 KiB** |
| 200 queries | **261 KiB** |
| a pyinstrument profile of a ~20 ms request | **+1.6 KiB** (a profile grows with the number of distinct stacks sampled; deep, long requests reach hundreds of KiB) |

So each SQL span costs about **1.3 KiB** on disk, most of it the SQL text and the
callsite (file, line, source line and up to 12 stack frames). With the defaults
(`max_rows = 10,000`) a store of 20-query traces settles around **300 MiB**; a store of
plain requests around 16 MiB. `max_spans_per_trace` (default 2000) bounds the largest
single trace at roughly 2.6 MiB.

The factors, largest first:

- the number of SQL spans, and the length of your SQL;
- captured bodies (up to `max_body_bytes` each, request and response);
- **profiles**, which can dominate for long requests;
- headers and tags.

To measure your own app: after a representative run, `ls -lh .redline/` (include the
`-wal` file) and divide by `storage.rows` from `GET api/meta`. Reproduce the table with
`uv run python benchmarks/storage.py`.

Disk usage after pruning does not shrink on its own: SQLite reuses freed pages but does not
return them to the operating system. To reclaim space, stop the app and run
`sqlite3 .redline/redline.db 'VACUUM'`, or delete the file.

## Looking inside

It is an ordinary SQLite database:

```bash
sqlite3 .redline/redline.db
```

```sql
.tables
SELECT method, route, count(*) AS n, round(avg(duration_ms), 1) AS avg_ms
FROM traces GROUP BY method, route ORDER BY n DESC LIMIT 10;

SELECT json_extract(body, '$.error.type'), count(*) FROM traces
WHERE json_extract(body, '$.error') IS NOT NULL GROUP BY 1;
```

Treat the file as **read-only**: the schema and the JSON layout are not a public
interface. Use the [HTTP API](http-api.md) for anything you build on.

## Backup and copying

You usually do not need to back it up. To take a copy of a running database consistently
use SQLite's online backup: `sqlite3 .redline/redline.db ".backup copy.db"`. Copying only
the `.db` file while it is being written can miss data that is still in the `-wal` file.

## A different store

The `Store` protocol is public. A Postgres store is planned for
[v1.0](roadmap.md). To plug in your own now, pass `install(store=...)` (FastAPI),
`Redline(store=...)` (Flask), `RedlinePlugin(store=...)` (Litestar) or set `REDLINE["STORE"]`
(Django); see [Extending](extending.md#the-store-protocol).

The 0.1 `Store` surface is unchanged, so a 0.1 custom store keeps working. The 0.2
features (history, the Database, Cache and External pages, load runs, live tail) live on an
**extended** protocol, `redline_core.storage.ExtendedStore`. A store that lacks a method, or
raises `NotImplementedError`, makes the matching API route answer `501 not_supported` and
the UI page say it is unavailable, while everything else works.
