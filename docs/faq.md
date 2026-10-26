# FAQ

[← Index](index.md)

## How finished is this?

**Alpha, private, and unpublished.** The version is `0.1.0a1`, the classifier is
`Development Status :: 3 - Alpha`, the repository is private, and **nothing is published**
to PyPI or GitHub Pages. Redline is the first of four planned parts, and only that first
part exists.

What that means in practice:

- **What is built.** Request tracing with span timelines; SQL, cache, template and
  outbound HTTP spans; N+1, duplicate, slow-query and query-budget detectors; fix
  suggestions for Django and SQLAlchemy; EXPLAIN with plan analysis for PostgreSQL, MySQL
  and SQLite; opt-in flamegraphs; redaction; a SQLite store; a JSON API; the web UI; both
  adapters, with demo shops for each.
- **It is tested.** The repository has roughly 250 test functions across the three
  packages (a `grep` count of `def test_`, so parametrised cases are more), including
  concurrency tests for the `contextvars` recorder, redaction, and the fix suggesters
  against real models. A CI workflow is defined for Python 3.10 to 3.14 and Django 5.2,
  6.0 and 6.1. PostgreSQL EXPLAIN tests skip when no server is available.
- **The API may still move.** Setting names, the JSON API shape and the extension protocols
  are not frozen. Pin an exact version.
- **There is no upgrade guarantee**: no migrations for the store, no deprecation cycle.
- **Measured on one machine.** Overhead benchmarks exist (`benchmarks/`) and were run on an
  Apple M2 Pro; see [Performance and overhead](performance.md#measured-numbers). Your
  application is not that benchmark, so A/B it.
- **Not yet proven in production.** It is designed to be usable there with sampling, but
  multi-process behaviour in particular has not been exercised. See
  [Production](production.md).
- **Not built.** Load testing ([v0.2](roadmap.md)), baselines and CI integration
  (v0.3), a shared Postgres store, Celery stitching, and everything else on the
  [roadmap](roadmap.md).
- **Not deployed.** The documentation is built locally
  (`uv run --with mkdocs-material mkdocs serve`).

Use it in development and staging first, and on an internal service second.

## Why is it not on PyPI?

The project is private by the owner's decision, and nothing is published. Install from Git,
naming both the adapter and `redline-core` in one command. See
[Getting started](getting-started.md#install).

## How is it different from django-silk?

django-silk is the best-known self-hosted Django profiler. Redline aims at the same job and
differs in the following, which are design choices, not criticism of a project that
solved a harder problem first.

| | django-silk | Redline |
|---|---|---|
| Frameworks | Django | Django and FastAPI |
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

Load testers generate traffic and see the application from outside. Redline sees inside
one request at a time. They answer different questions today. The point of
[v0.2](roadmap.md) is to join them. **Until that exists, Redline does no load testing at
all.**

## Does it slow my application down?

It adds work: on an Apple M2 Pro, about **0.04 to 0.06 ms per request plus 12 to 44 µs
per SQL query** while recording (a few µs in total when disabled). See
[Performance and overhead](performance.md) for the measurements, the cost model and what
each setting controls. The headline facts are that disk I/O is off the request path, the cost when
nothing is recording is a `ContextVar.get()`, and **`sample_rate` reduces analysis and
disk but not the per-query recording cost**.

## Is it safe to run in production?

It can be, with sampling, authentication, `explain_analyze` off, and retention limits. It
is an alpha, and not hardened. Read [Production](production.md) and
[Security](security.md) first. The safest posture is to run it in development and staging
and tunnel to a loopback-only instance when you need to look at production.

## Does it touch my database?

It **never writes** to it and never runs migrations. It reads from it only when you click
**Explain**: one `EXPLAIN` of a recorded `SELECT` (or `EXPLAIN ANALYZE` if you enabled it),
in a transaction that is always rolled back.

## Where does the data go?

To `.redline/redline.db` in a SQLite file, on the machine running your app. Nothing is sent
anywhere. See [Storage](storage.md).

## Can I use it with Flask / Litestar / Falcon?

Not out of the box. The `redline-core` engine is framework-free and you can drive it
yourself ([Extending: Using the core directly](extending.md#using-the-core-directly)), but
there is no adapter, and the web UI is served by an adapter. FastAPI-adjacent Starlette
apps work through `fastapi-redline` if you can call `install()` on a FastAPI app. A
plain-Starlette entry point is not offered.

## Can I use it with another ORM?

Recording works for any **SQLAlchemy** engine and any **Django database connection**.
Fix suggestions exist for the Django ORM and SQLAlchemy. For other ORMs you still get
detection and the SQL, and you can write a
[FixSuggester](extending.md#fixsuggester). Tortoise, Peewee or raw drivers are not
instrumented, because nothing hooks their cursors.

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

A GraphQL endpoint served over HTTP POST is recorded like any request (one route, so
Endpoints will group every query under it). **WebSockets are not recorded** on FastAPI.
gRPC is not supported.

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

## How do I delete everything?

**Clear all** in the UI, `DELETE api/traces`, `manage.py redline_clear` (Django), or delete
the `.redline/` directory while the app is stopped.

## What is the licence?

MIT, copyright 2026 Mahmoud Ahmed. See `LICENSE` in the repository.

## Where do I report a bug or a security problem?

Bugs: the repository's issue tracker (it is private; ask the owner for access).
Security: privately, see `SECURITY.md`. Contributing is in [Contributing](contributing.md).
