# Performance and overhead

[← Index](index.md)

A profiler that slows the thing it measures is worse than useless, so Redline is built to
keep its cost low and explainable. This page describes the **cost model**: where the time
goes and what you can do about it. It deliberately contains **no measured numbers yet**.

!!! warning "Benchmark numbers are to be measured"

    The project plan calls for published overhead measurements: Redline off, sampled and
    full, per request and per query. They have not been run. Until they are, this
    page makes **no claims** in milliseconds or percent. Anyone quoting a figure for
    Redline's overhead today is guessing. See [the placeholder section](#measured-numbers).

## Where the cost is

Work happens in four places. Each is controlled by a different setting.

### 1. Per request, always (unless ignored)

Starting and finishing a trace, creating the span list, the middleware's own bookkeeping,
and on FastAPI wrapping `receive` and `send` to copy body chunks (only up to
`max_body_bytes`).

Reduce it with `ignore_paths` (the request is not recorded at all) or `enabled: false`.

### 2. Per query, always (inside a recorded request)

For every SQL statement: two `perf_counter()` reads, the span object, the **SQL
fingerprint** (memoised, so repeated statements are cheap), a **stack walk** to find the
callsite (a cached filename verdict per frame, one `linecache` read for the innermost), and
**parameter redaction by column** (a regular expression over the statement).

This is the cost that **`sample_rate` does not reduce**: the decision to keep a trace is
made when the request ends, after these costs were paid. A request with 400 queries
pays 400 times. Hot, query-heavy endpoints are where Redline costs most, and also where
it is most useful.

### 3. Per kept trace

When the sampler keeps a trace: redaction of headers, query and bodies, running the
detectors, running the fix suggesters (the Django and SQLAlchemy ones parse source files
with `ast`, so they do real work per issue),
and JSON serialisation. Lowering `sample_rate` reduces this directly.

### 4. On the writer thread

SQLite inserts, with WAL. It competes for the GIL with your request threads, but it is off
the request path. The request only does a `put_nowait`.

### Plus, on demand

| Feature | Cost |
|---|---|
| Profiling a request | pyinstrument sampling every millisecond while the request runs; only for profiled requests |
| EXPLAIN | one query on your database, once, when someone clicks. `ANALYZE` runs the query for real |
| The UI polling | one `COUNT(*)` plus a page query every two seconds while a Requests page is open |

## What the design avoids

- **No disk I/O on the request path.** Saving is a queue put.
- **No cost when idle.** Every hook is `ContextVar.get()` and a `None` check outside a
  recorded request: your management commands, tests and ignored paths pay almost nothing.
- **No extra queries.** Redline never queries your application database while recording.
  (EXPLAIN does, on demand.)
- **No unbounded buffering of bodies.** Request and response bodies are copied up to
  `max_body_bytes` and no further, streaming responses included.
- **No tracing of itself.** Its own mount is ignored and its store is not one of your
  connections.

## What it does not avoid

- Memory proportional to the number of spans in the **in-flight** requests. There is no
  per-trace span cap in this version.
- A queue of up to `writer_queue_size` full traces in memory when the writer lags.
- Garbage: a span object and a dict per query.
- The callsite stack walk is a Python loop. Deep stacks cost more.

## How to keep it cheap

| Do | Effect |
|---|---|
| Ignore health checks, metrics and static assets | their requests are skipped entirely |
| Lower `sample_rate` | less analysis, serialising and disk; **not** less recording |
| Turn off `capture_response_body` | smaller traces, less redaction work, less memory |
| Lower `max_body_bytes` | less copying |
| Leave `profile` at `opt_in` | no profiler cost unless asked |
| Keep `max_rows` modest | a smaller file, and faster Issues and Endpoints queries |
| Set `project_root` correctly | callsite classification caches work; a wrong root means no callsites and wasted walks |

## Measured numbers

This section is reserved for results. The plan calls for a benchmark suite in a
`benchmarks/` directory (it does not exist in the repository yet). Once it has been run on
stated hardware this table will be filled in:

| Scenario | What will be measured | Status |
|---|---|---|
| Redline disabled (`enabled: false`) vs not installed | the cost of the middleware being present | **to be measured** |
| Redline recording, `sample_rate = 0` | per-request and per-query recording cost alone | **to be measured** |
| Redline recording, `sample_rate = 1` | recording plus analysis plus write | **to be measured** |
| Query-heavy request (hundreds of queries) | how cost scales with query count | **to be measured** |
| Profiled request | pyinstrument overhead | **to be measured** |
| Writer saturation | the request rate at which `dropped` starts rising | **to be measured** |

The *target* in the project plan is **under 1 ms plus a few microseconds per query when
not profiling**. It is a target, not a result.

### Measure it yourself

Until then, A/B your own application with a representative load:

1. Run the app with `enabled: false`, record requests per second and latency percentiles
   with a load tool you trust (Locust, k6, `wrk`, `hey`).
2. Run again with Redline on at the settings you intend for production.
3. Compare latency percentiles and CPU, not just the mean.
4. Check `storage.dropped` in `GET api/meta` after the run: it must be `0`.
5. Repeat with a query-heavy endpoint, since that is the worst case.

## Why this page exists anyway

The cost model tells you *which setting to reach for* when something is slow, which does
not depend on the numbers. When the benchmarks are run, the table above will say how
much.
