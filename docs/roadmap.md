# Roadmap

[← Index](index.md)

Redline has four parts: **Profile**, **Analyze**, **Stress** and **Compare/CI**. Version
0.1 is Profile and Analyze. The rest is planned, **not built**. This page is the plan, not a
promise: it is the intent as of 0.1.0a1, and it will change as the earlier parts meet real
use.

| Version | Theme | Status |
|---|---|---|
| **0.1** | Profile and Analyze: traces, N+1 with fix diffs, EXPLAIN, flamegraphs, UI, Django and FastAPI | **Built** (alpha) |
| 0.2 | **Stress**: load testing correlated with in-app traces | Planned |
| 0.3 | **Compare / CI**: baselines, diffs, a pytest plugin, PR comments | Planned |
| 1.0 | Hardened production mode and the long tail | Planned |

## The thesis

Load testers (Locust, k6, Gatling) only see an application from the outside. They tell
you the p95 doubled at 150 users. They cannot tell you **which query** went from 3 ms to
410 ms. Profilers like Redline see the inside but only one request at a time. No
open-source tool connects the two. That connection is the reason the roadmap is shaped
the way it is.

The foundations are already in 0.1: traces carry the reserved tags `run_id` and `step`,
and the `X-Redline-Run: <id>;step=<n>` request header is parsed today into those tags (you
can filter on them: `GET api/traces?tag=run_id:abc`). The load generator that sends the
header does not exist yet.

## v0.2: Stress

Load testing, **correlated with the traces Redline already records**.

- A **`redline load` CLI** and a UI page. The load generator runs in **separate
  processes** (asyncio and httpx workers), so it does not distort the app it measures.
- An **open-model arrival-rate scheduler** (requests arrive at a rate you set, whether or
  not earlier ones finished), recorded with **HdrHistogram** and corrected for
  **coordinated omission**: the classic mistake that makes closed-loop load tests report
  flattering latencies.
- A **step-ramp mode** that finds the **knee**: RPS against p95 against errors, as a
  chart.
- **Scenarios** generated from the Django URLconf, from the FastAPI OpenAPI schema, or
  from captured traffic (redacted).
- **Run correlation**: requests carry `X-Redline-Run: <id>;step=<n>`, so in-app traces are
  tagged and the UI can show the **bottleneck per step**: *query X: 3 ms at 10 users, 410
  ms at 150 users*.
- **Saturation signals**: database pool wait and lock detection (`pg_locks`), and for
  FastAPI event-loop lag and threadpool saturation.

## v0.3: Compare and CI

Make performance a thing you can regress-test.

- **Runs and baselines** tagged with the git SHA, with **diffs per endpoint** and
  **flamegraph diffs**.
- A **pytest plugin**: `@pytest.mark.redline(max_queries=..., no_n_plus_one=True)`, so a
  test fails when a view regresses to an N+1.
- **`redline ci`** emits **JUnit** output and a **Markdown PR comment** (`+queries`,
  `+p95`, the culprit line).

## v1.0

The long tail.

- **Hardened production mode** with a **Postgres store**, replacing today's
  single-node SQLite caveats.
- **Celery and RQ trace stitching**, so a request and the background work it spawned
  appear together.
- A **memory-per-request** extra (tracemalloc or memray).
- In-process **fault injection**: latency or errors per endpoint or query.
- **Index what-if** through HypoPG: test a suggested index without creating it.
- **OTLP import and export**.
- Optional **AI diagnosis** (bring your own key): from the trace, SQL, plan and source to a
  ranked diagnosis and a patch.
- **Folding `django-signal-tracer` in** as a panel.

## What is likely to be missing for a while

Be realistic about gaps that are not on the list above:

- Frameworks other than Django and FastAPI/Starlette (Flask, Litestar, Falcon).
- ORMs other than the Django ORM and SQLAlchemy (you can write a
  [FixSuggester](extending.md#fixsuggester)).
- Non-SQL data stores (Redis commands beyond Django's cache API, MongoDB).
- Cross-service distributed tracing.
- Alerting.
- Publishing to PyPI. The repository is private and nothing is published; that is a
  separate decision.

## How the roadmap is decided

Each version gets its own detailed plan before work starts, and each stays small. The
outline above is from the project plan; details, ordering and scope inside each version
may change.
