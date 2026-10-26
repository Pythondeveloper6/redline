# Redline overhead benchmarks

- Date: 2026-10-26
- Python: 3.13.15 (CPython) on Darwin arm64
- CPU: Apple M2 Pro
- Packages: django 6.1.2, fastapi 0.162.0, sqlalchemy 2.1.6, pyinstrument 5.1.3, redline-core 0.1.0a1
- Method: 1500 timed requests per cell after warm-up, 3 interleaved rounds pooled; each cell in a fresh subprocess; in-process client, in-memory SQLite.
- Queries per request: 0 (fixed cost) and 20 (adds 20 queries).

## django

| mode | median ms (0 q) | p95 ms (0 q) | median ms (20 q) | p95 ms (20 q) | overhead / request (0 q) | overhead / query |
|---|---|---|---|---|---|---|
| off | 0.039 | 0.045 | 0.107 | 0.124 | baseline | baseline |
| disabled | 0.043 | 0.050 | 0.119 | 0.141 | +0.004 ms | +0.4 us |
| sample0 | 0.078 | 0.093 | 0.397 | 0.422 | +0.039 ms | +12.5 us |
| full | 0.102 | 0.137 | 0.618 | 1.132 | +0.063 ms | +22.4 us |
| profile | 0.234 | 0.285 | 1.140 | 1.499 | +0.195 ms | +41.9 us |

## fastapi

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

## Storage size and writer throughput (`benchmarks/storage.py`)

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
