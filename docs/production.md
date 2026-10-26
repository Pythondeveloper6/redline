# Production

[← Index](index.md)

Redline was designed first for development, where recording everything is exactly right.
It can be run in production, with care, and this page is the care. The short version:
**sample, always keep the slow and the failed, cap retention, put the UI behind real
authentication, and leave the expensive features off.**

!!! warning "Not production-hardened yet"

    This is an alpha. The hardened production mode (a shared Postgres store, tested
    multi-process behaviour) is planned for [v1.0](roadmap.md). Everything below is
    about making a single-node, SQLite-backed install as safe as it can be. If you cannot
    afford a surprise, start in staging, watch the numbers on
    [Performance and overhead](performance.md), and enable it for a fraction of traffic.

## The recipe

=== "Django"

    ```python
    DEBUG = False

    REDLINE = {
        "SAMPLE_RATE": 0.05,                    # 5% of requests...
        "ALWAYS_KEEP_SLOWER_THAN_MS": 500,      # ...plus every slow one...
        "ALWAYS_KEEP_ERRORS": True,             # ...plus every failure
        "CAPTURE_RESPONSE_BODY": False,         # smaller store, less data at rest
        "MAX_ROWS": 5_000,
        "MAX_AGE_DAYS": 2,
        "PROFILE": "opt_in",                    # flamegraphs only when asked
        "EXPLAIN_ANALYZE": False,
        "STORAGE_PATH": "/var/lib/myapp/redline.db",
        "PERMISSION": "myproject.redline.is_superuser",
    }
    ```

=== "FastAPI"

    ```python
    install(
        app,
        engine=engine,
        settings={
            "sample_rate": 0.05,
            "always_keep_slower_than_ms": 500,
            "always_keep_errors": True,
            "capture_response_body": False,
            "max_rows": 5_000,
            "max_age_days": 2,
            "profile": "opt_in",
            "explain_analyze": False,
            "storage_path": "/var/lib/myapp/redline.db",
        },
        dependencies=[Depends(require_superuser)],
    )
    ```

## Sampling

With `sample_rate = 0.05` and the safety nets on, you store:

| Request | Stored? | `sampled_by` |
|---|---|---|
| a fast success, 5% of the time | yes | `rate` |
| a fast success, otherwise | **no** | n/a |
| anything slower than `always_keep_slower_than_ms` | yes | `slow` |
| an unhandled exception or a 5xx | yes | `error` |
| a request you profiled by hand | yes | `profile` |

That is the right trade for finding problems: the failures and the outliers are all there,
and the typical request is represented by a sample.

!!! warning "What sampling does *not* reduce"

    Sampling is applied **when the request ends**. Every non-ignored request still pays
    the recording cost as it runs: timing each query, fingerprinting, capturing the
    callsite, redacting query parameters. Only the **analysis** (detectors, suggestions,
    header and body redaction), the **serialising** and the **disk write** are skipped
    for dropped requests. To remove the cost entirely for a path, add it to `ignore_paths`,
    and to turn the whole thing off, set `enabled` to false.

    So `sample_rate = 0.01` is not "1% of the overhead". Measure.

### Dev default versus production

| | Development | Production |
|---|---|---|
| `sample_rate` | `1.0` | `0.01` to `0.1` |
| Request and response bodies | captured | request bodies only if you need them; consider **off** |
| Profiling | `opt_in`, or `all` for a session | `opt_in` or `off` |
| `explain_analyze` | on if you like | **off** |
| Access | you, on localhost | a superuser or SSO-protected role |

Django does the first part for you: with `DEBUG = False` and no `SAMPLE_RATE`, the default
is `0.1`. Setting `1.0` with `DEBUG = False` triggers the warning
`django_redline.W005`.

## Retention

Two limits, both applied by the writer thread every 200 saved traces and by
`manage.py redline_prune`.

| Setting | Effect | Note |
|---|---|---|
| `max_age_days` | delete older than this | `0` or `None` disables the age rule |
| `max_rows` | keep only the newest N | `0` or `None` disables the row cap |

Size depends on what you store. Measured (`benchmarks/storage.py`): about **1.6 KiB** for
a trace with no queries, **31 KiB** with 20 queries and **260 KiB** with 200, roughly
1.3 KiB per SQL span. See [Storage: size](storage.md#size). The ceiling you set with
`max_rows` times the size of your *largest* typical trace is your upper bound, and
`max_spans_per_trace` (default 2000) bounds the largest trace.

## What to leave off

| Feature | Why not in production |
|---|---|
| `profile: "all"` | Every request pays the profiler's overhead |
| `explain_analyze: True` | It **executes** the query on your production database, on demand, as whoever can reach the UI |
| `capture_response_body: True` | Response bodies are the biggest and most sensitive thing stored. Keep them off unless you are debugging a specific problem |
| `sample_rate: 1.0` | The cost scales with traffic and the store churns |
| Opening the UI to the internet | See [Security](security.md#who-can-open-the-ui) |

## What NOT to do

- **Do not leave the FastAPI default access in front of a proxy.** The loopback check
  sees the proxy as localhost. Pass real `dependencies`.
- **Do not skip authentication** on the grounds that it is "just a profiler". It shows
  SQL, request bodies and runs EXPLAIN.
- **Do not point `storage_path` at your application database file.** Always use a
  separate path. Use a writable volume that **survives restarts** if you want history,
  and expect to lose it if the container filesystem is ephemeral (which is fine).
- **Do not run multiple replicas against one shared network file.** SQLite over NFS and
  similar is unreliable. Each instance should have its own local file.
- **Do not treat Endpoints percentiles as your SLO dashboard.** They are computed on a
  sample that over-represents slow requests. See
  [Issues and endpoints](issues-and-endpoints.md).
- **Do not forget that data on disk is data you hold.** Request bodies and SQL parameters
  contain whatever your users sent, minus the keys that matched the redaction patterns.
  Check it covers your fields.

## Process models

**Django.** The runtime (settings, store, writer thread) is built **lazily** on first use
in each process, not at import, so it is safe with gunicorn's pre-fork model and with
management commands (`migrate` never creates `.redline/`). Each worker process gets its
own writer thread writing to the same file (see below).

**FastAPI.** `install()` creates the store when it is called, typically at import time.
The writer thread starts lazily, on the first trace saved in each process.

**Fork-safe in both.** The store notices when it is used in a new process
(`os.register_at_fork`, plus a PID check on every call) and opens its own SQLite
connection, queue and writer thread there; the connection inherited from the parent is
never used or closed in the child. So gunicorn `--preload`, uWSGI without `lazy-apps`
and `multiprocessing` with the fork start method all work: traces recorded in a worker
are written by that worker. This is covered by a test that forks a process with a running
writer. With `uvicorn --workers N` each worker imports the app itself anyway.

**Several workers.** All workers write to the **same SQLite file**. SQLite serialises
writers (WAL mode, a 10-second busy timeout), and each worker prunes on its own schedule,
so counts and retention are approximate. This is not covered by the test suite. If you
want per-worker isolation, give each worker its own `storage_path` (for example with the
PID in it), at the cost of having to look at each file separately.

## Graceful shutdown

Pending traces are written by a background thread, so a hard kill loses the last
second or so.

- **FastAPI:** the wrapped lifespan flushes and closes the store on shutdown (it waits for
  the queue, up to 5 seconds).
- **Django:** an `atexit` hook flushes and closes the runtime.

`SIGKILL` skips both. That is acceptable for a profiler.

## Checklist

- [ ] `sample_rate` is below 1 and the always-keep rules are on
- [ ] UI access is authenticated by something stronger than "it is on localhost"
- [ ] `explain_analyze` is off
- [ ] `profile` is `opt_in` or `off`
- [ ] `max_rows` and `max_age_days` are set to what you can afford
- [ ] `storage_path` is outside the app database and on local disk, and `.redline/` is in
      `.gitignore`
- [ ] You checked what your request bodies contain against `redact_keys`
- [ ] `ignore_paths` includes health checks, metrics endpoints and anything chatty
- [ ] You looked at [Performance and overhead](performance.md) and measured your own app
- [ ] (Django) `manage.py check --deploy` and `manage.py check` are clean, including the
      `django_redline.*` checks

## Watching Redline itself

`GET api/meta` (also at `<mount>/api/meta`) reports `storage.rows` and `storage.dropped`.
Alert on `dropped` rising: it means the writer cannot keep up and you are losing traces.
Redline's own problems are logged on the `redline` logger. Route it to your logging setup.
