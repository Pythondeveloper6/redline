# Redline benchmarks

## v0.2 fixed-cost fixes (2026-10-27)

Medians of three `uv run python benchmarks/overhead.py --quick` runs ("full" mode, per-request
fixed cost at 0 queries against the `off` baseline), same machine, before and after:

| framework | before | after | per query before / after |
|---|---|---|---|
| fastapi | +0.085 ms | +0.054 ms | +50.3 / +45.9 us |
| django | +0.070 ms | +0.058 ms | +27.4 / +33.0 us (quick-mode noise: runs ranged 26-33 us) |
| flask | +0.036 ms | +0.033 ms | +28.6 / +26.8 us |
| litestar | +0.055 ms | +0.047 ms | +48.1 / +55.4 us (noisy) |

What changed: FastAPI 0.16x matches an included router by walking each of its routes, and
Redline's sit first, so every application request paid ~5-7 us to be told "not Redline";
they are now skipped with one `startswith` unless the path is under the prefix. Ids
(`new_id()`, two or more per request) read `os.urandom` in 4 KiB batches instead of one
syscall each. The FastAPI serializer span walks the stack for its callsite only when it is
slow enough to become a `slow_serializer` issue. The event-loop monitor, detection and the
storage hand-off measured at noise level each and were left as they are. Access checks run
only on Redline's own routes (they are route dependencies), so they never cost an app request.

## v0.2 (current)

- Date: 2026-10-27
- Python: 3.14.7 (CPython) on Darwin arm64 (v0.1 below ran on 3.13.15)
- CPU: Apple M2 Pro
- Packages: django 6.1.2, fastapi 0.162.0, flask 3.1.3, litestar 2.25.0, sqlalchemy 2.1.6, pyinstrument 5.1.3, redline-core 0.1.0a1 (v0.2 development tree)
- Method: same as v0.1 (1500 timed requests per cell, 3 interleaved rounds pooled, fresh subprocess per cell, in-process client, in-memory SQLite), now for four frameworks. Flask is driven with `app.test_client()`, Litestar with raw ASGI calls like FastAPI.
- Run twice, back to back (`overhead.py` then `storage.py`, twice). The machine was not idle: other agent sessions were building and testing on it, so absolute numbers carry noise. The two runs agree within about 2-4% on every overhead figure, so the tables below are run 1 in full and the headline table lists both runs. Run 2 is in the headline table rather than being averaged away, so the spread is visible.
- Caveat on comparing with v0.1: the Python minor version differs (3.14 vs 3.13) and the machine was shared, so differences of a few microseconds are not attributable to Redline changes. The `off` baselines moved too (Django 0 q: 0.039 -> 0.041 ms), which is a rough measure of the drift.

### Headline (run 1 / run 2; "full" mode, overhead against the `off` baseline)

| framework | per-request fixed cost | v0.1 | per-query cost | v0.1 | disabled cost / request | disabled / query |
|---|---|---|---|---|---|---|
| django | +0.071 / +0.069 ms | +0.063 | +28.0 / +29.6 us | +22.4 | +0.004 / +0.006 ms | +0.4 / +0.3 us |
| fastapi | +0.089 / +0.091 ms | +0.048 | +50.1 / +51.3 us | +44.1 | +0.007 / +0.008 ms | +0.4 / +0.1 us |
| flask | +0.038 / +0.040 ms | new | +28.2 / +28.0 us | new | +0.001 / +0.003 ms | +0.2 / +0.1 us |
| litestar | +0.058 / +0.058 ms | new | +50.9 / +49.7 us | new | +0.000 / -0.000 ms (noise) | +0.3 / -0.1 us |

Reading it: Django and FastAPI fixed and per-query costs are higher than the v0.1 run (FastAPI fixed cost +0.089 vs +0.048 ms is the largest move). Part of that may be the Python 3.14 / loaded-machine confound above, but it is consistent across both v0.2 runs, so treat it as a regression candidate and re-measure on 3.13 before attributing it. Flask is the cheapest adapter per request (the SQLAlchemy listener cost per query matches Django's); Litestar and FastAPI, which share the ASGI path and a SQLAlchemy engine inside an async handler, cost about the same as each other. The disabled path stays at noise level everywhere (Flask and Litestar install nothing when `enabled=False`). In Flask `full` is almost the same as `sample0`: the storage write happens on the background thread, and this harness does not measure the GIL contention it causes (not investigated).

- Date: 2026-10-27
- Python: 3.14.7 (CPython) on Darwin arm64
- CPU: Apple M2 Pro
- Packages: django 6.1.2, fastapi 0.162.0, flask 3.1.3, litestar 2.25.0, sqlalchemy 2.1.6, pyinstrument 5.1.3, redline-core 0.1.0a1
- Method: 1500 timed requests per cell after warm-up, 3 interleaved rounds pooled; each cell in a fresh subprocess; in-process client, in-memory SQLite.
- Queries per request: 0 (fixed cost) and 20 (adds 20 queries).

### django

| mode | median ms (0 q) | p95 ms (0 q) | median ms (20 q) | p95 ms (20 q) | overhead / request (0 q) | overhead / query |
|---|---|---|---|---|---|---|
| off | 0.041 | 0.050 | 0.111 | 0.126 | baseline | baseline |
| disabled | 0.046 | 0.056 | 0.123 | 0.138 | +0.004 ms | +0.4 us |
| sample0 | 0.083 | 0.098 | 0.449 | 0.533 | +0.042 ms | +14.8 us |
| full | 0.112 | 0.192 | 0.741 | 1.141 | +0.071 ms | +28.0 us |
| profile | 0.263 | 0.329 | 1.633 | 2.003 | +0.222 ms | +65.0 us |

### fastapi

| mode | median ms (0 q) | p95 ms (0 q) | median ms (20 q) | p95 ms (20 q) | overhead / request (0 q) | overhead / query |
|---|---|---|---|---|---|---|
| off | 0.027 | 0.032 | 0.341 | 0.372 | baseline | baseline |
| disabled | 0.034 | 0.040 | 0.355 | 0.401 | +0.007 ms | +0.4 us |
| sample0 | 0.089 | 0.108 | 0.966 | 1.154 | +0.061 ms | +28.2 us |
| full | 0.117 | 0.144 | 1.431 | 1.693 | +0.089 ms | +50.1 us |
| profile | 0.369 | 0.431 | 3.065 | 3.526 | +0.342 ms | +119.1 us |

### flask

| mode | median ms (0 q) | p95 ms (0 q) | median ms (20 q) | p95 ms (20 q) | overhead / request (0 q) | overhead / query |
|---|---|---|---|---|---|---|
| off | 0.081 | 0.093 | 0.412 | 0.456 | baseline | baseline |
| disabled | 0.082 | 0.100 | 0.417 | 0.471 | +0.001 ms | +0.2 us |
| sample0 | 0.118 | 0.134 | 0.996 | 1.149 | +0.037 ms | +27.3 us |
| full | 0.119 | 0.147 | 1.014 | 1.269 | +0.038 ms | +28.2 us |
| profile | 0.298 | 0.366 | 1.995 | 2.243 | +0.217 ms | +68.3 us |

### litestar

| mode | median ms (0 q) | p95 ms (0 q) | median ms (20 q) | p95 ms (20 q) | overhead / request (0 q) | overhead / query |
|---|---|---|---|---|---|---|
| off | 0.016 | 0.020 | 0.326 | 0.362 | baseline | baseline |
| disabled | 0.016 | 0.020 | 0.333 | 0.370 | +0.000 ms | +0.3 us |
| sample0 | 0.052 | 0.062 | 0.987 | 1.227 | +0.036 ms | +31.3 us |
| full | 0.074 | 0.086 | 1.402 | 1.773 | +0.058 ms | +50.9 us |
| profile | 0.253 | 0.289 | 2.872 | 3.338 | +0.237 ms | +115.5 us |

Overhead = median with Redline minus median of the `off` baseline, same query count.
Per-query overhead = (overhead at 20 queries - overhead at 0 queries) / 20.

Overhead = median with Redline minus median of the `off` baseline, same query count.
Per-query overhead = (overhead at 20 queries - overhead at 0 queries) / 20.

### Storage size and writer throughput (v0.2, `benchmarks/storage.py`, run 1 / run 2)

| queries / trace | bytes / trace on disk (v0.2) | v0.1 | writer throughput, traces/s (v0.2) | v0.1 |
|---|---|---|---|---|
| 0 | 2.3 KiB / 2.3 KiB | 1.6 KiB | 31,938 / 29,018 | 53,635 |
| 20 | 31.5 KiB / 31.5 KiB | 30.9 KiB | 3,180 / 3,644 | 3,803 |
| 200 | 265.3 KiB / 265.3 KiB | 260.9 KiB | 401 / 392 | 442 |

- Profile (pyinstrument, ~20 ms request): 1.8 KiB of speedscope JSON per profiled trace (v0.1: 1.6 KiB); whole profiled trace 33.8 / 33.7 KiB (v0.1: 32.8 KiB).
- Memory per in-flight SQL span (tracemalloc, 2000 queries in one request): 1,549 bytes (v0.1: 1,543); default `max_spans_per_trace = 2000` caps a request at about 3.0 MiB of spans.
- Bytes per trace grew about 0.7 KiB for a trace with no queries (not investigated; likely extra fields recorded on each trace in v0.2) and 2% at 20-200 queries. Writer throughput for tiny traces fell by about 40-45% (53.6k -> 29-32k traces/s); that is the same 0-query row where the per-trace record grew, and Python 3.14 vs 3.13 is also in play. At 20 and 200 queries the writer is 4-14% slower. All are far above any realistic request rate for one process.

### Load generator capacity (`redline load`)

Measured with `redline load` (httpx client, open model, `--steps`, `--no-store`) against a hello-world ASGI app (200 OK, 2-byte body) on uvicorn with 3 server workers, `--no-access-log`, on the same machine, keep-alive. The server was not the limit: `ab -c 64 -k` reached 21,200 rps against it from one thread. Server and client share the 10+ cores of the M2 Pro with whatever else was running.

"Sustained" is the highest step at which the client sent exactly the target rate with nothing dropped and low latency. Latency is the client's own measurement per request (so it includes the loopback server round trip, about 0.5 ms). Past the knee the client is saturated: arrivals queue, `max_inflight` (1000) fills, requests are dropped, and p50 jumps to 1-2 s. Those post-knee latencies measure the backlog, not the server.

| workers | max sustained rps (no drops) | p50 / p99 at that step | steps beyond it |
|---|---|---|---|
| 1 | 1,400 (step 9 of the 600-1,400 sweep; p95 already 698 ms) ; 1,300 fully clean | 0.64 ms / 0.97 ms at 1,300 | achieved 760-970 rps with drops once the target is 1,600+ |
| 2 | 1,800 (p99 281 ms, p50 0.68 ms); 1,600 fully clean | 0.70 ms / 1.03 ms at 1,600 | achieved 1,350-1,600 rps above 2,000 |
| 4 | 3,000 (earlier sweep) | 0.95 ms / 1.54 ms | achieved 2,300-3,100 rps; 2,420 max sustained in the repeat sweep |

Takeaways: about 1,300 rps per worker process, scaling less than linearly (1.4x for 2 workers, 2.3x for 4) because the workers, the server and other work compete for cores. Behaviour is bimodal: below the knee latency is under 2 ms, above it the run collapses, which `redline load` reports as a knee (`achieved < 90% of target`). Two things to know: starting a sweep above a worker's capacity (e.g. step 1 at 2,000 rps with one worker) never recovers, since the open-model backlog never drains, so start low and step up; and repeat sweeps varied by 10-25% at the edge (the 4-worker knee was 3,000 in one sweep and 2,500 in the next) because of the shared machine. For load tests above about 1,000 rps per process, add workers; the default of up to 8 is more than a laptop needs.

Reproduce:

```
uv run uvicorn hello:app --port 18080 --workers 3 --no-access-log   # hello.py: bare ASGI 200 "ok"
uv run redline load hello.toml --steps 800:3000:200 --step-duration 5s --workers 2 --no-store
```

---

## v0.1

- Date: 2026-10-26
- Python: 3.13.15 (CPython) on Darwin arm64
- CPU: Apple M2 Pro
- Packages: django 6.1.2, fastapi 0.162.0, sqlalchemy 2.1.6, pyinstrument 5.1.3, redline-core 0.1.0a1
- Method: 1500 timed requests per cell after warm-up, 3 interleaved rounds pooled; each cell in a fresh subprocess; in-process client, in-memory SQLite.
- Queries per request: 0 (fixed cost) and 20 (adds 20 queries).

### django

| mode | median ms (0 q) | p95 ms (0 q) | median ms (20 q) | p95 ms (20 q) | overhead / request (0 q) | overhead / query |
|---|---|---|---|---|---|---|
| off | 0.039 | 0.045 | 0.107 | 0.124 | baseline | baseline |
| disabled | 0.043 | 0.050 | 0.119 | 0.141 | +0.004 ms | +0.4 us |
| sample0 | 0.078 | 0.093 | 0.397 | 0.422 | +0.039 ms | +12.5 us |
| full | 0.102 | 0.137 | 0.618 | 1.132 | +0.063 ms | +22.4 us |
| profile | 0.234 | 0.285 | 1.140 | 1.499 | +0.195 ms | +41.9 us |

### fastapi

| mode | median ms (0 q) | p95 ms (0 q) | median ms (20 q) | p95 ms (20 q) | overhead / request (0 q) | overhead / query |
|---|---|---|---|---|---|---|
| off | 0.025 | 0.027 | 0.321 | 0.340 | baseline | baseline |
| disabled | 0.031 | 0.035 | 0.335 | 0.360 | +0.006 ms | +0.4 us |
| sample0 | 0.053 | 0.057 | 0.861 | 0.905 | +0.028 ms | +25.6 us |
| full | 0.073 | 0.080 | 1.251 | 1.796 | +0.048 ms | +44.1 us |
| profile | 0.254 | 0.287 | 2.373 | 2.837 | +0.229 ms | +91.2 us |

Overhead = median with Redline minus median of the `off` baseline, same query count.
Per-query overhead = (overhead at 20 queries - overhead at 0 queries) / 20.

Notes:

- Reproduce with `uv run python benchmarks/overhead.py` (about 4 minutes). Absolute numbers are
  machine specific; the `off` baseline is a bare view with no middleware, so Redline's share
  looks larger here than in a real stack with a database server and templates.
- `sample0` still records every span (that is the cost of deciding later whether to keep a
  trace); only analysis, redaction of bodies and storage are skipped.
- `full` storage writes happen on a background thread; the table counts only what the
  request thread pays, plus GIL contention seen by the timed requests.
- The FastAPI baseline query is a SQLAlchemy Core `text()` select (about 15 us); its
  per-query overhead includes SQLAlchemy event-listener dispatch.
- `disabled` on FastAPI installs neither the middleware nor the SQLAlchemy listeners
  (before this run it installed both, which cost +4.6 us per query); what remains is the
  mounted UI routes being matched first.
- The span cap (`max_spans_per_trace`, default 2000) is not reached at 20 queries, so
  these numbers are the uncapped path.

### Storage size and writer throughput (`benchmarks/storage.py`)

- Date: 2026-10-26
- Python: 3.13.15 (CPython) on Darwin arm64
- Reproduce with `uv run python benchmarks/storage.py` (about a minute).
- Shape: browser-like headers, `?page=2`, a JSON response of about 1 KB, every
  query from one loop line (so 20 and 200 queries carry an N+1 issue with its
  fix suggestion). Request bodies off (a GET). Retention off while measuring.

| queries / trace | traces | bytes / trace on disk | issues |
|---|---|---|---|
| 0 | 2000 | 1.6 KiB | none |
| 20 | 2000 | 30.9 KiB | n_plus_one |
| 200 | 500 | 260.9 KiB | n_plus_one |

Profile (pyinstrument, ~20 ms request): 1.6 KiB of speedscope JSON per profiled trace (whole profiled trace: 32.8 KiB).

| queries / trace | writer throughput (traces/s) |
|---|---|
| 0 | 53,635 |
| 20 | 3,803 |
| 200 | 442 |

Memory per in-flight SQL span (tracemalloc, 2000 queries in one request): 1,543 bytes; the default `max_spans_per_trace = 2000` caps a request at about 2.9 MiB of spans.

## v0.4 SQL Server-shaped overhead (2026-10-29)

The `mssql` cell drives Flask -> Redline hooks -> a view that runs 20 SQLAlchemy Core
queries through a **fake pyodbc-shaped DBAPI** (`benchmarks/fake_mssql_dbapi.py`). It has
the same `cursor.execute`/`fetchall`/`nextset` call shape as pyodbc and reports
`db.system=mssql`, but no network or SQL Server is involved. It measures Redline's
instrumentation cost, not SQL Server latency.

- Command: `uv run python benchmarks/overhead.py --framework mssql`
- 1500 timed requests per cell after warm-up, 3 interleaved rounds pooled
- Python 3.13.15 on Darwin arm64, Apple M2 Pro, SQLAlchemy 2.1.6

| mode | 0 q median | 20 q median | overhead / request | overhead / query |
|---|---|---|---|---|
| off | 0.078 ms | 0.312 ms | baseline | baseline |
| disabled | 0.078 ms | 0.311 ms | +0.000 ms | -0.1 us |
| sample0 | 0.131 ms | 1.373 ms | +0.053 ms | +50.4 us |
| full | 0.130 ms | 1.383 ms | +0.052 ms | +50.9 us |
| profile | 0.320 ms | 2.661 ms | +0.242 ms | +105.3 us |

`full` is within 0.052 ms of the report's 0.1 ms fixed-cost target. Its 50.9 us/query is
0.9 us above the 50 us target in this run; treat that as measurement-level rather than a
guarantee. `profile` is intentionally outside that budget (pyinstrument sampling).
The same optimizations also reduced the Flask cell; they are exercised by the SQLAlchemy
event path, not by the fake driver specifically.

The benchmark's `--framework` option selects cells, so this one can be rerun without
running all four adapters.
