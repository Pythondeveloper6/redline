# Performance and overhead

[← Index](index.md)

A profiler that slows the thing it measures is worse than useless, so Redline is built to
keep its cost low and explainable. This page describes the **cost model** (where the time
goes and what you can do about it) and the [measured numbers](#measured-numbers).

!!! abstract "In one line"

    Measured on an Apple M2 Pro (Python 3.14, 2026-10-27, v0.2 development tree): recording
    every request costs about **0.04 to 0.09 ms per request plus 28 to 51 µs per SQL
    query**, depending on the framework; disabled, it is a few µs per request and under
    1 µs per query. The v0.1 figures (0.05 to 0.06 ms and 22 to 44 µs) are kept
    [below](#v01-numbers) for comparison. Details and how to reproduce are
    [below](#measured-numbers).

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

### What 0.2 adds to the cost model

The 0.2 features were designed to cost nothing when idle. Their cost, by where it falls:

| Feature | When it costs | What it costs |
|---|---|---|
| Event-loop lag monitor ([Blocking in async](async-blocking.md)) | only while a request is in flight on a loop | a timer callback every 5 ms per busy loop; a watchdog thread that sleeps until a tick is overdue. Nothing in an idle process. `detect_blocking = False` removes it |
| Row counts and `has_limit` on SQL spans | per query | reading `rowcount` and one scan of the SQL text; no extra query |
| Transaction and pool-wait spans | per transaction / checkout | one span each |
| Log capture ([Logs](logs-and-exceptions.md)) | per log record inside a request | formatting, redaction and a callsite lookup; one `ContextVar.get()` outside a request |
| Exception locals | only when a request raises | walking the traceback once; nothing on the happy path |
| Derived tables (`trace_sql`, `trace_ext`, `trace_cache`, rollups, issue index) | per **kept** trace, on the **writer thread** | more rows per batch in the same transaction. The request thread is not involved |
| `Server-Timing` header | per recorded response | formatting one string |
| Badge injection | per recorded HTML response in debug mode | one string insert before `</body>` |
| Contrib integrations (`requests`, redis, asyncpg, psycopg) | per call inside a request | one `ContextVar.get()` outside a request; a span inside |
| The aggregate pages and History | when a page is opened | queries over small summary tables, not over trace bodies |
| Load testing | while a run is going | the generator runs in separate **processes**; run it from another machine if you want its CPU kept off the app under test |

The [measured numbers](#measured-numbers) below include v0.2 with all of the above active
(the harness exercises the recorder end to end). The individual additions are small
per-span or per-batch costs that were **not benchmarked one by one**. If you need a number
for your app, use [Measure it yourself](#measure-it-yourself).

### Plus, on demand

| Feature | Cost |
|---|---|
| Profiling a request | pyinstrument sampling every millisecond while the request runs; only for profiled requests |
| EXPLAIN | one query on your database, once, when someone clicks. `ANALYZE` runs the query for real |
| The UI polling | one `COUNT(*)` plus a page query every two seconds while a Requests page is open and Live mode is on polling (Live mode normally uses one open SSE connection instead) |
| `api/stream` | one open connection per viewer, closed after five minutes on ASGI servers and about 25 seconds on WSGI servers, where it holds a worker thread (the client reconnects and resumes by `seq`) |
| A snapshot download | building one HTML file (the UI bundle, inlined) |

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

## Memory

- Each in-flight SQL span costs about **1.5 KB** (measured, including its callsite stack).
  A request stores at most `max_spans_per_trace` spans (default **2000**, about 3 MB);
  beyond that spans are counted but not kept, so a runaway loop of 100,000 queries does
  not hold 150 MB. Query counts, query time, and the N+1 and slow-query issues still
  include the dropped spans (per fingerprint and callsite counters), and the trace is
  marked **truncated** in the UI. `0` or `None` removes the cap.
- A queue of up to `writer_queue_size` full traces in memory when the writer lags.
- Garbage: a span object and a dict per query (beyond the cap, a short-lived one).
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

### v0.2 numbers

From `benchmarks/RESULTS.md`, measured on **2026-10-27** against the v0.2 development tree
(`redline-core 0.1.0a1` version string), Apple M2 Pro, Python 3.14.7 (CPython, macOS
arm64), Django 6.1.2, FastAPI 0.162.0, Flask 3.1.3, Litestar 2.25.0, SQLAlchemy 2.1.6,
pyinstrument 5.1.3. Method as in v0.1: 1500 timed requests per cell, three interleaved
rounds pooled, a fresh subprocess per cell, an in-process client, in-memory SQLite. The
`off` baseline is a bare view, so Redline's *share* looks larger than it would next to a
real database. The run was repeated back to back; the two runs agree within 2 to 4%, and
the table shows run 1 with run 2 where they differ visibly.

**Added time per request** (median, 0 queries) and **per SQL query**, recording with
`sample_rate = 1` (every trace kept), against Redline not installed:

| Framework | Per request (v0.2) | v0.1 | Per query (v0.2) | v0.1 | Disabled, per request / query |
|---|---|---|---|---|---|
| Django | +0.071 ms | +0.063 ms | +28.0 µs | +22.4 µs | +0.004 ms / +0.4 µs |
| FastAPI | +0.089 ms | +0.048 ms | +50.1 µs | +44.1 µs | +0.007 ms / +0.4 µs |
| Flask | +0.038 ms | new | +28.2 µs | new | +0.001 ms / +0.2 µs |
| Litestar | +0.058 ms | new | +50.9 µs | new | about 0 / +0.3 µs |

Other modes (run 1): with `sample_rate = 0` (nothing kept) the per-request / per-query cost
is +0.042 ms / 14.8 µs (Django), +0.061 ms / 28.2 µs (FastAPI), +0.037 ms / 27.3 µs (Flask)
and +0.036 ms / 31.3 µs (Litestar). Profiling every request (`profile: "all"`) costs
+0.222 / +0.342 / +0.217 / +0.237 ms per request and +65 / +119 / +68 / +116 µs per query,
in that order.

Read the comparison with care. Django and FastAPI are somewhat more expensive than in v0.1
(FastAPI's fixed cost +0.089 vs +0.048 ms is the largest move). Part of it may be Python
3.14 against 3.13 and a shared machine (the `off` baselines drifted by a few microseconds),
but it is consistent across both runs, so treat it as a candidate regression until it has
been re-measured on 3.13. Flask is the cheapest adapter per request; Litestar and FastAPI
share the ASGI path and cost about the same.

Storage (`benchmarks/storage.py`, v0.2 against v0.1):

| Queries / trace | On disk per trace | v0.1 | Writer, traces/s | v0.1 |
|---|---|---|---|---|
| 0 | 2.3 KiB | 1.6 KiB | about 29,000 to 32,000 | 53,600 |
| 20 | 31.5 KiB | 30.9 KiB | about 3,200 to 3,600 | 3,800 |
| 200 | 265.3 KiB | 260.9 KiB | about 400 | 440 |

A profile adds 1.8 KiB of speedscope JSON per profiled trace. An in-flight SQL span costs
1,549 bytes, so the default `max_spans_per_trace = 2000` caps a request at about 3 MiB of
spans. Tiny traces are about 0.7 KiB bigger and the writer is slower for them (not
investigated), still far above any realistic rate for one process.

**Load generator** (`redline load`, httpx, open model, against a hello-world ASGI app on
the same machine): about 1,300 requests per second per worker process without drops, scaling
less than linearly (about 1,600 to 1,800 with 2 workers, about 2,400 to 3,000 with 4).
Beyond a worker's capacity the open-model backlog never drains and the run collapses, which
`redline load` reports as a knee, so start a sweep low and step up. Repeat sweeps varied by
10 to 25% at the edge. For more than about 1,000 requests per second per process add
`--workers`.

### v0.1 numbers

From `benchmarks/RESULTS.md`, measured on **2026-10-26** against `0.1.0a1`, Apple M2 Pro, Python 3.13.15
(CPython, macOS arm64), Django 6.1.2, FastAPI 0.162.0, SQLAlchemy 2.1.6, pyinstrument
5.1.3. Each cell is 1500 timed requests after warm-up, in a fresh subprocess, through an
in-process client against in-memory SQLite. The baseline (`off`) is a bare view with no
middleware, so Redline's *share* looks larger than it would next to a real database and
templates; the absolute added time is what carries over.

**Added time per request** (median, 0 queries) and **per SQL query**, against Redline not
installed:

| Mode | Django / request | Django / query | FastAPI / request | FastAPI / query |
|---|---|---|---|---|
| `enabled: false` | +0.004 ms | +0.4 µs | +0.006 ms | +0.4 µs |
| recording, `sample_rate = 0` (nothing kept) | +0.039 ms | +12.5 µs | +0.028 ms | +25.6 µs |
| recording, `sample_rate = 1` (every trace kept) | +0.063 ms | +22.4 µs | +0.048 ms | +44.1 µs |
| `profile: "all"` (a flamegraph on every request) | +0.195 ms | +41.9 µs | +0.229 ms | +91.2 µs |

The FastAPI per-query figures include SQLAlchemy's event dispatch around a ~15 µs
`text()` query. A disabled FastAPI install adds no middleware and no SQLAlchemy listeners.

| Scenario | Result |
|---|---|
| Disabled vs not installed | a few µs per request, under 1 µs per query |
| Recording with `sample_rate = 0` | the per-query recording cost alone: 12.5 µs (Django), 25.6 µs (FastAPI) |
| Recording with `sample_rate = 1` | adds analysis, redaction and the enqueue: 22.4 µs / 44.1 µs per query |
| Query-heavy request | linear in query count: at the `sample_rate = 1` rates, 200 queries add about 4.5 ms (Django) or 9 ms (FastAPI); beyond `max_spans_per_trace` the cost per query drops (no redaction, no storage) |
| Profiled request | about 0.2 ms per request plus roughly double the per-query cost while pyinstrument samples |
| Writer saturation | the writer stores about **3,800 traces/s** of 20 queries each (about 440/s at 200 queries, 50,000/s with none) on the same machine; `dropped` only rises above that rate of *kept* traces (`benchmarks/storage.py`) |

The plan's target was "under 1 ms plus a few microseconds per query when not profiling".
The per-request part is met by a wide margin; the per-query part is **12 to 51 µs** across
both releases, more than "a few", mostly spent on fingerprinting, callsite capture and
parameter redaction.

### Reproduce

```bash
uv sync --all-packages
uv run python benchmarks/overhead.py     # about 4 minutes; --quick for a smoke run
uv run python benchmarks/storage.py      # about a minute: sizes and writer throughput
```

Absolute numbers are machine specific; compare modes on the same machine.

### Measure it yourself

The benchmark is a bare view. To know the cost in your application, A/B it with a
representative load:

1. Run the app with `enabled: false`, record requests per second and latency percentiles
   with a load tool you trust (`redline load`, Locust, k6, `wrk`, `hey`).
2. Run again with Redline on at the settings you intend for production.
3. Compare latency percentiles and CPU, not just the mean.
4. Check `storage.dropped` in `GET api/meta` after the run: it must be `0`.
5. Repeat with a query-heavy endpoint, since that is the worst case.

## Why this page exists anyway

The cost model tells you *which setting to reach for* when something is slow, which does
not depend on the numbers. The measurements say how much, on one machine; your own A/B
says how much on yours.
