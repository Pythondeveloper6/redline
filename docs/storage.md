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
| FastAPI | `.redline/redline.db`, relative to the **working directory** at start |
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

There is **no schema version and no migration mechanism** in this alpha. If a later
alpha changes the schema and the file will not open, delete `.redline/` (it is only
history).

## The writer thread

`SQLiteStore.save(trace)` puts the trace on a bounded queue (`writer_queue_size`, default
1000) and returns. A daemon thread named `redline-writer`:

1. takes one item, then drains up to **50** more that are already waiting (a batch);
2. writes the batch in **one transaction**;
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
- `SQLiteStore(background=False)` writes inline on the calling thread. That is for tests
  and short scripts, **not** for serving requests.

## Retention

Two rules, applied together.

| Rule | Setting | Behaviour |
|---|---|---|
| Age | `max_age_days` (default 7) | delete traces whose start is older than this. `0` disables the age rule |
| Count | `max_rows` (default 10,000) | keep the newest N, delete the rest. `0` means keep none |

Applied by the writer every 200 saved traces, by `manage.py redline_prune` (Django) on
demand, and when you call `store.prune(max_age_s=..., max_rows=...)`. Pruning removes the
trace and its profile, tags and issue rows together. **Clear all** (`DELETE api/traces`)
and `manage.py redline_clear` remove everything.

Because retention is checked per 200 writes, the store can briefly exceed `max_rows` by up
to roughly that many.

!!! note "Retention shapes the Issues and Endpoints pages"

    Both pages only know about traces that still exist. A 7-day window with
    `max_age_days = 2` shows two days. A `max_rows` that holds an hour of traffic makes a
    `24h` window show an hour.

## Size

!!! warning "To be measured"

    No sizes are quoted here because none have been measured yet. The factors are:

    - the number of spans (every SQL query is a span with its SQL text and parameters);
    - captured bodies (up to `max_body_bytes` each, request and response);
    - the callsite source line and stack on each SQL span;
    - **profiles**, by far the largest single item.

    Measure your own app: after a representative run, look at `ls -lh .redline/` (include
    the `-wal` file) and divide by `storage.rows` from `GET api/meta`. A number will go
    on this page when the [benchmarks](performance.md) are run.

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
[v1.0](roadmap.md). To plug in your own now (FastAPI), see
[Extending](extending.md#the-store-protocol).
