# FAQ

[← Index](index.md)

## How finished is this?

**Alpha, and open source.** The version is `0.2.0a1`, the classifier is
`Development Status :: 3 - Alpha`, and the code is on GitHub under the MIT licence. It is
**not on PyPI yet**: you install it from GitHub (see [Getting started](getting-started.md#install)).
Redline has four planned parts; the first two (Profile and Analyze) and the third (Stress)
exist, and the fourth (Compare / CI) is next.

What that means in practice:

- **What is built.** Request tracing with span timelines; SQL, cache, template, serializer,
  transaction, GraphQL resolver and outbound HTTP spans; N+1, duplicate, slow-query,
  query-budget, blocking-in-async, transaction, result-size, count/exists, serializer and
  external-call detectors; fix suggestions for Django and SQLAlchemy; EXPLAIN with plan
  analysis for PostgreSQL, MySQL and SQLite; opt-in flamegraphs; logs and exception locals;
  redaction; a SQLite store with release-aware history; a load tester with knee detection
  and per-step bottlenecks; a static N+1 scan; one access-control model for all frameworks;
  a JSON API; the web UI; adapters for **Django (including Ninja), FastAPI, Flask and
  Litestar**, with demo apps.
- **It is tested.** The repository has a large test suite across the five packages,
  including concurrency tests for the `contextvars` recorder, redaction, and the fix
  suggesters against real models. CI covers Python 3.10 to 3.14 and Django 5.2, 6.0 and 6.1.
  PostgreSQL EXPLAIN tests skip when no server is available.
- **The API may still move.** Setting names, the JSON API shape and the extension protocols
  are not frozen. Pin an exact version or commit.
- **Upgrades are best-effort.** The SQLite store migrates itself in place and a 0.1 file is
  backfilled (see [Storage](storage.md#migration)), but there is no deprecation cycle yet.
- **Measured on one machine.** Overhead benchmarks exist (`benchmarks/`) and were run on an
  Apple M2 Pro against 0.1; see [Performance and overhead](performance.md#measured-numbers).
  Your application is not that benchmark, so A/B it.
- **Not yet proven in production.** It is designed to be usable there with the
  [production preset](production.md#the-production-preset), but multi-process behaviour in
  particular has had little real-world exercise.
- **Not built.** Baselines in CI and a pytest plugin (v0.3), a shared Postgres store,
  Celery stitching, and everything else on the [roadmap](roadmap.md).

Use it in development and staging first, and on an internal service second.

## Why is it not on PyPI?

Publishing a package under a name is a commitment to maintain it, and the project is still
in alpha. For now you install from GitHub, naming both the adapter and `redline-core` in one
command so pip can satisfy the adapter's pin. See
[Getting started](getting-started.md#install). Publishing to PyPI is a later decision.

## How is it different from django-silk?

django-silk is the best-known self-hosted Django profiler. Redline aims at the same job and
differs in the following, which are design choices, not criticism of a project that
solved a harder problem first.

| | django-silk | Redline |
|---|---|---|
| Frameworks | Django | Django (and Ninja), FastAPI, Flask, Litestar |
| Async | no async support | recorded through `contextvars`; ASGI and WSGI, sync and async views |
| Where traces live | your application database | a separate SQLite file |
| Secrets | request data is stored as captured | masked before storage, by default |
| N+1 | you compare query counts | detected, grouped, with the line that causes it |
| Fix | none | a unified diff for Django and SQLAlchemy |
| EXPLAIN | none | one click, safe by default, with index suggestions |
| Flamegraphs | cProfile call tree | pyinstrument, async-safe, exportable to speedscope |

What silk has that Redline does not: a longer history, a larger user base, and
Django-specific features this alpha lacks. If silk does what you need and your data is not
sensitive, it may be enough.

## How is it different from django-debug-toolbar?

The toolbar is a superb **in-page, per-request, development** tool: you see one request's
panels inside the page you are looking at. Redline is a **persistent record** with a UI of
its own: it works for requests that are not HTML pages (APIs, JSON, htmx, background
calls), keeps the last few thousand, aggregates across them (Endpoints, Issues) and groups
repeated problems. They are complementary and can both be installed. The toolbar's
interactive panels (templates, settings, signals, headers) are things Redline does not try
to replicate.

## How is it different from Sentry or Logfire?

Those are **hosted observability platforms**: distributed tracing across services,
alerting, fleet-wide dashboards, retention measured in weeks, and a vendor to pay and trust
with your data. They are the right tool for **production** monitoring across a system.

Redline is **self-hosted and local**: the data never leaves your disk, it knows your ORM
well enough to say which line to change, and it costs nothing to run. It does **not** do
cross-service tracing, alerting or long-term retention. Used together, the APM tells you a
route got slow and Redline tells you the query behind it.

## How is it different from Locust or k6?

Load testers generate traffic and see the application from outside. Redline's profiler
sees inside one request at a time. Since 0.2 Redline also has a **load tester**
(`redline load`) that joins the two: it drives your app at a controlled rate with an open
model, corrects for coordinated omission, finds the knee, and shows which query or endpoint
got slower at each step. See [Load testing](load-testing.md).

It is deliberately smaller than Locust or k6: HTTP only, scenario files instead of
scripting, no distributed generators, no browsers or WebSockets. If you need those, use them
and tag the requests with `X-Redline-Run` so Redline can still correlate.

## Does it slow my application down?

It adds work: on an Apple M2 Pro, about **0.04 to 0.06 ms per request plus 12 to 44 µs
per SQL query** while recording (a few µs in total when disabled), measured on 0.1. The 0.2
features add small per-span or per-batch costs and are idle when unused. See
[Performance and overhead](performance.md) for the measurements, the cost model and what
each setting controls. The headline facts are that disk I/O is off the request path, the
cost when nothing is recording is a `ContextVar.get()`, and **`sample_rate` reduces analysis
and disk but not the per-query recording cost**.

## Is it safe to run in production?

It can be, with the `production` preset (sampling, token access, read-only, no bodies, no
locals), `explain_analyze` off, and retention limits. It is an alpha, and not hardened. Read
[Production](production.md), [Access control](access-control.md) and
[Security](security.md) first. The safest posture is to run it in development and staging
and tunnel to a loopback-only or token-protected instance when you need to look at
production.

## Does it touch my database?

It **never writes** to it and never runs migrations. It reads from it only when you click
**Explain**: one `EXPLAIN` of a recorded `SELECT` (or `EXPLAIN ANALYZE` if you enabled it),
in a transaction that is always rolled back.

## Where does the data go?

To `.redline/redline.db` in a SQLite file, on the machine running your app. Nothing is sent
anywhere. See [Storage](storage.md).

## Can I use it with Flask / Litestar / Falcon?

Flask and Litestar: yes, since 0.2. See [Flask](flask.md) and [Litestar](litestar.md). Django
Ninja is served by the Django adapter ([Django Ninja](django-ninja.md)). Falcon and others
have no adapter. The `redline-core` engine is framework-free and you can drive it yourself
([Extending: Using the core directly](extending.md#using-the-core-directly)), but the web UI
is served by an adapter. A plain-Starlette entry point is not offered; Starlette apps work
through `fastapi-redline` if you can call `install()` on a FastAPI app.

## Can I use it with another ORM?

Recording works for any **SQLAlchemy** engine and any **Django database connection**, and
for raw **asyncpg** and **psycopg 3** through [integrations](integrations.md). Fix
suggestions exist for the Django ORM and SQLAlchemy. For other ORMs you still get detection
and the SQL if the driver is instrumented, and you can write a
[FixSuggester](extending.md#fixsuggester). Tortoise and Peewee are not instrumented, because
nothing hooks their cursors.

## Does it work with async Django?

Yes. The middleware is both sync and async capable, SQL goes through
`connection.execute_wrapper`, and context propagates through `sync_to_async`. There are
tests for async views. See [Django](django.md#async-views).

## Does it work with Django REST Framework?

Yes: requests are traced like any other. For N+1s, the fix suggester knows serializers: if a
serializer reads a relation for every object, the diff targets the view's `queryset` or
`get_queryset()`. See [Django: Django REST Framework](django.md#django-rest-framework).

## Does it work with Celery, Django-Q, RQ or other background workers?

Only if you drive the recorder yourself (see
[Extending](extending.md#using-the-core-directly)). Celery and RQ stitching, so a request
and its tasks appear together, is planned for [v1.0](roadmap.md).

## Does it support GraphQL, gRPC or WebSockets?

**GraphQL:** yes, with Strawberry and Graphene. Each field resolution is a span, and an N+1
is reported against the resolver path (`orders.*.customer`). See [GraphQL](graphql.md).
Without the extension, a GraphQL endpoint is recorded like any POST (one route).
**WebSockets are not recorded.** gRPC is not supported.

## Why does my N+1 show no diff?

The suggester found the relation but not the source expression: it lives in another module,
is built dynamically, or `project_root` is wrong. You still get a code hint at lower
confidence. See [N+1 detection: when the suggester cannot help](n-plus-one.md#when-the-suggester-cannot-help).

## Why is something reported as an N+1 that is not?

The detector counts the same read from the same line at least `n_plus_one_threshold`
(default 5) times. A loop that runs 5 legitimately different-by-design lookups, or a
cache-warming loop, looks identical. Raise the threshold, or accept the report as a
prompt to check. Tuning is in [N+1 detection](n-plus-one.md#tuning).

## Why are there no requests in the UI?

See the checklist in [Getting started](getting-started.md#look-at-it): ignored path, a
missing middleware or `install()`, a sample rate below 1, or the writer lagging a second.

## Why do I see no SQL?

FastAPI: the engine was not passed to `install()` or `add_engine()`. Django: the
database connection was opened before Redline was installed in a way that bypassed
`connection_created` (unusual; Redline also instruments connections already open at
startup). In both: the work ran in a bare thread, which is not attributed to a request.

## Can I turn it off without removing it?

Yes: `enabled: false` (`REDLINE["ENABLED"] = False`). On Django the middleware stays but
passes every request straight through; on FastAPI `install()` then adds no middleware and
no SQLAlchemy listeners at all. Measured, a disabled install costs a few µs per request and
under 1 µs per query. The UI still shows what is already stored.

## Can I record only some requests?

Yes: `ignore_paths` excludes by prefix or regex, and `sample_rate` samples. There is no
per-user or per-header filter in this version.

## Does it work on Windows?

The code is pure Python and uses no platform-specific calls that we know of, but CI runs on
Linux and the project has been developed on macOS. Treat Windows as untested.

## Does it support PostgreSQL / MySQL / SQLite?

Recording works with any database SQLAlchemy or Django can talk to. EXPLAIN and plan
analysis support **PostgreSQL, MySQL (including MariaDB) and SQLite**; `EXPLAIN ANALYZE`
only on PostgreSQL. Other databases record SQL but cannot be explained.

## Does it work with Django Ninja?

Yes, through `django-redline`. Ninja's per-path view is resolved to the real operation, so
the trace shows your endpoint function and the route aggregates per method. See
[Django Ninja](django-ninja.md).

## How does it find blocking calls in an async app?

An event-loop lag monitor runs only while a request is in flight, and a watchdog thread samples
the loop's stack when a tick is late. The issue names the call (`time.sleep`, `requests.get`,
sync ORM). See [Blocking in async](async-blocking.md).

## Can I compare two releases?

Yes: hourly rollups and an issue index are kept for 90 days, and the History page compares
any two releases or time ranges with a per-endpoint verdict. See [History](history.md).

## Is the load tester safe to point at production?

No. It is an attack tool by nature. Run it against your own staging or local environment.
Starting runs from the UI is off by default and can only target the app serving the UI. See
[Load testing](load-testing.md) and [Security](security.md#load-from-the-ui).

## Can I put it behind a reverse proxy or in Kubernetes?

Yes, with a token and `trusted_proxies`. See the recipes in
[Access control](access-control.md#recipes).

## How do I configure it per environment?

Presets (`dev`, `staging`, `production`), a `redline.toml`, `REDLINE_*` environment
variables, and your framework's settings are layered. `redline config` shows the result.
See [Configuration](configuration.md).

## How do I delete everything?

**Clear all** in the UI, `DELETE api/traces`, `manage.py redline_clear` (Django), or delete
the `.redline/` directory while the app is stopped.

## What is the licence?

MIT, copyright 2026 Mahmoud Ahmed. See `LICENSE` in the repository.

## Where do I report a bug or a security problem?

Bugs: the repository's [issue tracker](https://github.com/Pythondeveloper6/redline/issues).
Security: privately, see `SECURITY.md`. Contributing is in [Contributing](contributing.md).
