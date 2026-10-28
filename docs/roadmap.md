# Roadmap

[← Index](index.md)

Redline has four parts: **Profile**, **Analyze**, **Stress** and **Compare/CI**. Versions 0.1
and 0.2 built the first three. Compare/CI has started: its first piece, the
[refactor check](refactor-check.md), is built in 0.3.0a1. The rest of it is planned, **not
built**. This page is the plan, not a promise: it is the intent as of 0.2.0a1, and it will
change as the earlier parts meet real use.

| Version | Theme | Status |
|---|---|---|
| **0.1** | Profile and Analyze: traces, N+1 with fix diffs, EXPLAIN, flamegraphs, UI, Django and FastAPI | **Built** |
| **0.2** | Analyze more, **Stress**, History: Flask and Litestar, a load tester, new detectors, release history, access control | **Built** (alpha) |
| 0.3 | **Compare / CI**: refactor checks, endpoint budgets, pytest and source analysis | Built in the development branch; integration and release validation in progress |
| 1.0 | Hardened production mode and the long tail | Planned |

## The thesis

Load testers (Locust, k6, Gatling) only see an application from the outside. They tell
you the p95 doubled at 150 users. They cannot tell you **which query** went from 3 ms to
410 ms. Profilers like Redline see the inside but only one request at a time. No
open-source tool connected the two. 0.2 does: see [Load testing](load-testing.md).

## v0.2: what shipped

Everything below is built and documented. The [changelog](changelog.md) has the detail.

- **Five frameworks**: Django (and Ninja), FastAPI, [Flask](flask.md), [Litestar](litestar.md).
- **New analysis**: [blocking in async](async-blocking.md), [transactions and locks](transactions.md),
  [large and unbounded results](result-size.md), count/exists, [slow serializers](serializers.md),
  [external calls](external-calls.md), [cache efficiency](cache-efficiency.md),
  [logs and exception locals](logs-and-exceptions.md), [GraphQL](graphql.md) resolvers.
- **[Database](database.md)**: SQL search, a workload index advisor, unused indexes.
- **[Static N+1 scan](static-scan.md)** for templates and DRF serializers.
- **[Load testing](load-testing.md)**: `redline load`, an open-model scheduler corrected for
  coordinated omission, step ramps, knee detection, per-step bottlenecks, scenarios generated
  from OpenAPI or captured traffic.
- **[History](history.md)**: releases, hourly rollups, before/after compare, new and resolved
  issues, 90-day retention.
- **UI**: command palette, keyboard, open in editor, copy as cURL/HTTPie, replay, compare,
  Live mode, snapshots, the floating badge, Server-Timing.
- **[Configuration](configuration.md) and [access control](access-control.md)**: presets,
  `redline.toml`, `REDLINE_*`, tokens, basic auth, IP allow-lists, trusted proxies, read-only
  mode, feature switches.

## v0.3: Compare and CI

Make performance a thing you can regress-test.

- A **pytest plugin** (built in the development branch): `@pytest.mark.redline(max_queries=..., no_n_plus_one=True)`, so a
  test fails when a view regresses to an N+1.
- **`redline ci`** (planned) emits **JUnit** output and a **Markdown PR comment** (`+queries`, `+p95`,
  the culprit line), comparing a run against a stored baseline.
- **Flamegraph diffs** between two requests or two releases are deferred to the next release.
- **OTLP import and export** (planned), so traces can flow to and from other tools.
- Baselines and diffs per endpoint in CI, building on the History comparison already in 0.2.
- **Refactor check** (**shipped in 0.3.0a1**, see [Refactor check](refactor-check.md)): pick a
  set of read-only endpoints, record a baseline before a refactor, run it again after, and
  get a per-endpoint report of performance changes (time, queries, issues, overall average)
  and of response changes (a diff that ignores fields that change on every call). From the UI
  and from the command line, with an exit code, JUnit and a Markdown PR comment for CI.
- **`redline analyze`**: a static analysis of the application's Python code (not only its
  templates) that finds likely bottlenecks before any request runs: queries inside loops
  (N+1), missing `select_related` / `prefetch_related` / `selectinload`, unbounded
  queries, blocking calls inside async views, and similar database and logic problems.
  Extends the template scan shipped in 0.2.

See [Performance workflows](performance-workflows.md) for the implemented UI, assertions, source analysis and detector additions. Two live targets, differential flamegraphs, serial-call concurrency advice and automated cold/warm scenarios are deferred to the next release.

## v1.0

The long tail.

- **Hardened production mode** with a **Postgres store**, replacing today's single-node
  SQLite caveats.
- **Celery and RQ trace stitching**, so a request and the background work it spawned appear
  together.
- A **memory-per-request** extra (tracemalloc or memray).
- In-process **fault injection**: latency or errors per endpoint or query.
- **Index what-if** through HypoPG: test a suggested index without creating it.
- **Lock-wait detection** from `pg_locks`, and deeper pool and threadpool saturation signals.
- Optional **AI diagnosis** (bring your own key): from the trace, SQL, plan and source to a
  ranked diagnosis and a patch.
- **Folding `django-signal-tracer` in** as a panel.

## What is likely to be missing for a while

Be realistic about gaps that are not on the list above:

- Frameworks other than Django, FastAPI, Flask and Litestar (Falcon, Sanic, Pyramid).
- ORMs other than the Django ORM and SQLAlchemy (you can write a
  [FixSuggester](extending.md#fixsuggester)).
- Non-SQL data stores beyond Redis and Django's cache API (MongoDB).
- Cross-service distributed tracing.
- Alerting.
- Distributed load generation, browsers and WebSockets in the load tester.

## How the roadmap is decided

Each version gets its own detailed plan before work starts, and each stays small. The
outline above is from the project plan; details, ordering and scope inside each version may
change. Issues and ideas are welcome on the
[issue tracker](https://github.com/Pythondeveloper6/redline/issues).
