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
| off | 0.039 | 0.047 | 0.107 | 0.123 | baseline | baseline |
| disabled | 0.044 | 0.053 | 0.120 | 0.268 | +0.005 ms | +0.4 us |
| sample0 | 0.081 | 0.100 | 0.406 | 0.535 | +0.042 ms | +12.9 us |
| full | 0.103 | 0.195 | 0.657 | 1.120 | +0.064 ms | +24.3 us |
| profile | 0.233 | 0.263 | 1.123 | 1.473 | +0.194 ms | +41.1 us |

## fastapi

| mode | median ms (0 q) | p95 ms (0 q) | median ms (20 q) | p95 ms (20 q) | overhead / request (0 q) | overhead / query |
|---|---|---|---|---|---|---|
| off | 0.025 | 0.029 | 0.321 | 0.338 | baseline | baseline |
| disabled | 0.034 | 0.039 | 0.422 | 0.454 | +0.009 ms | +4.6 us |
| sample0 | 0.052 | 0.059 | 0.854 | 0.944 | +0.027 ms | +25.3 us |
| full | 0.071 | 0.082 | 1.247 | 1.824 | +0.046 ms | +44.1 us |
| profile | 0.252 | 0.302 | 2.409 | 2.975 | +0.227 ms | +93.1 us |

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
  per-query overhead includes SQLAlchemy event-listener dispatch (`disabled` shows the cost
  of the listeners existing at all).
