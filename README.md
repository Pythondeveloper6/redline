<div align="center">

<a href="https://pythondeveloper6.github.io/redline/">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img alt="Redline" src="docs/assets/logo-light.svg" height="64">
  </picture>
</a>

**A request profiler, backend analyzer and load tester for Django, FastAPI, Flask and Litestar.**<br>
See inside every request: N+1 queries with a fix diff, one-click EXPLAIN, flamegraphs, and a
load test that shows which query broke.

[![Python](https://img.shields.io/badge/python-3.10%2B-e5392f?logo=python&logoColor=white)](https://github.com/Pythondeveloper6/redline)
[![Django](https://img.shields.io/badge/django-5.2%20%7C%206.0%20%7C%206.1-0c4b33?logo=django&logoColor=white)](https://github.com/Pythondeveloper6/redline/tree/main/packages/django-redline)
[![FastAPI](https://img.shields.io/badge/fastapi-0.110%2B-009688?logo=fastapi&logoColor=white)](https://github.com/Pythondeveloper6/redline/tree/main/packages/fastapi-redline)
[![Flask](https://img.shields.io/badge/flask-3.0%2B-000000?logo=flask&logoColor=white)](https://github.com/Pythondeveloper6/redline/tree/main/packages/flask-redline)
[![Litestar](https://img.shields.io/badge/litestar-2.12%2B-202235)](https://github.com/Pythondeveloper6/redline/tree/main/packages/litestar-redline)
[![CI](https://github.com/Pythondeveloper6/redline/actions/workflows/ci.yml/badge.svg)](https://github.com/Pythondeveloper6/redline/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-e5392f?logo=opensourceinitiative&logoColor=white)](https://github.com/Pythondeveloper6/redline/blob/main/LICENSE)

[**Documentation**](https://pythondeveloper6.github.io/redline/) ·
[Getting started](https://pythondeveloper6.github.io/redline/getting-started/) ·
[Changelog](CHANGELOG.md) ·
[FAQ](https://pythondeveloper6.github.io/redline/faq/)

</div>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screenshots/django-n-plus-one-dark.png">
  <img alt="Redline showing an N+1 on order items in the Django demo shop: 30 queries from one line, with a prefetch_related diff" src="docs/assets/screenshots/django-n-plus-one-light.png">
</picture>

> **Alpha (`0.2.0a1`).** Working and tested, but the API may still move, and it is **not on
> PyPI yet**: you install from GitHub. See the
> [FAQ](https://pythondeveloper6.github.io/redline/faq/#how-finished-is-this) for an honest
> account of what is and is not done. Use it in development and staging first, and read
> [Production](https://pythondeveloper6.github.io/redline/production/) before pointing it at
> real traffic.

## Why

Request profilers split into two camps. In-page toolbars show one request, in development,
and forget it. Hosted APMs see production but not your source lines. The self-hosted middle
is thin: django-silk is Django-only and has no async story, and FastAPI, Flask and Litestar
have nothing comparable. Load testers only see an application from the outside.

Redline is built for that middle. It keeps a **persistent record** of recent requests, in
your own process and on your own disk, it knows enough about your ORM to say **which line to
change**, and it can push load at the same app and show **which query got slow at which
rate**.

- **Async-correct.** State lives in `contextvars`, so concurrent requests, `sync_to_async`
  and Starlette's threadpool all attribute to the right request.
- **Fix, not just a finding.** An N+1 is mapped back to your models and the queryset in
  your source, and written out as a unified diff.
- **Safe by default.** Secrets are redacted before storage. EXPLAIN runs only on recorded
  `SELECT`s, in a rolled-back transaction. One access policy for every framework: local-only
  by default, with tokens, basic auth, IP allow-lists, read-only mode and a production
  preset when you need them.
- **Out of your way.** Its own SQLite file, never your database. A background writer, so no
  disk I/O on the request path. No runtime dependencies in the core.

## How it compares

| | **Redline** | django-silk | django-debug-toolbar | Kolo | Sentry / Logfire | Locust / k6 |
|---|---|---|---|---|---|---|
| Django / FastAPI / Flask / Litestar | All four | Django | Django | Django | All | n/a |
| Async-correct | Yes | No async support | Partial | n/a | Yes | n/a |
| Persists across requests | Own SQLite file | In your app DB | History panel | Local | Hosted | Results only |
| N+1 grouped and deduplicated | Yes | No | Markers | Repeats shown | Yes | No |
| Fix as a diff | **Yes** | No | No | No | No | No |
| Names the call blocking the event loop | **Yes** | No | No | No | Partial | No |
| EXPLAIN + index advice | Yes | No | EXPLAIN only | No | No | No |
| Flamegraphs | Opt-in | cProfile tree | Optional panel | Call traces | Profiling products | No |
| Secrets redacted on write | Yes | No | n/a | Local | Configurable | n/a |
| Before/after across releases | Yes | No | No | No | Yes | Results only |
| Built for production fleets | **No** | No | No | No | **Yes** | n/a |
| Distributed tracing | **No** | No | No | No | **Yes** | n/a |
| Load testing | **Yes**, with the culprit query per step | No | No | No | No | **Yes**, more mature |

Where a cell says "No" for another tool it means "not as a built-in feature", and tools
change: check theirs. Redline does **not** replace a hosted APM for fleet-wide monitoring or
a mature distributed load generator. More in the
[docs](https://pythondeveloper6.github.io/redline/#how-it-compares).

## Install

Redline is not on PyPI yet. Install from GitHub, and **name `redline-core` in the same
command**: each adapter pins the exact core it was tested with (`redline-core==0.2.0a1`),
and pip cannot find that on PyPI, so without the second requirement the install fails.

```bash
# Django
pip install \
  "django-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/django-redline" \
  "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"

# FastAPI
pip install \
  "fastapi-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/fastapi-redline" \
  "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"

# Flask
pip install \
  "flask-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/flask-redline" \
  "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"

# Litestar
pip install \
  "litestar-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/litestar-redline" \
  "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"
```

Installing an adapter alone fails with `Could not find a version that satisfies the
requirement redline-core==0.2.0a1`. Do not "fix" that by installing `redline-core` from
PyPI: this project does not own the name there.

Extras go on the core requirement, for example
`"redline-core[profile,load] @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"`
for flamegraphs (`profile`) and the load tester (`load`). Integration extras: `httpx`,
`requests`, `redis`, `asyncpg`, `psycopg`, `strawberry`, `graphene`.

To hack on it or run the demos, clone the repository and run `uv sync --all-packages`.

Python 3.10+ for the core, FastAPI, Flask and Litestar adapters, 3.12+ for Django; Django
5.2, 6.0 or 6.1; FastAPI 0.110+, Flask 3.0+, Litestar 2.12+ with SQLAlchemy 2.x.

## Quickstart

### Django

```python
# settings.py
INSTALLED_APPS = [..., "django_redline"]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django_redline.middleware.RedlineMiddleware",   # as high as possible
    ...,
]

# urls.py
from django.urls import include, path

urlpatterns = [..., path("redline/", include("django_redline.urls"))]
```

Run your app, make some requests, and open **`/redline/`** as a superuser.
`python manage.py check` tells you if anything is miswired. Django Ninja and DRF work with
no extra setup.

### FastAPI

```python
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from fastapi_redline import install

app = FastAPI()
engine = create_async_engine("sqlite+aiosqlite:///app.db")

# access="local": localhost only (unset, Redline answers 404 outside debug mode)
install(app, engine=engine, settings={"access": "local"})  # UI at http://localhost:8000/_redline/
```

### Flask

```python
from flask import Flask
from sqlalchemy import create_engine
from flask_redline import Redline

app = Flask(__name__)
engine = create_engine("sqlite:///app.db")

# access="local": localhost only (unset, Redline answers 404 outside debug mode)
Redline(app, engine=engine, settings={"access": "local"})  # UI at http://localhost:5000/_redline/
```

With Flask-SQLAlchemy, omit the engine: its engines are found automatically.

### Litestar

```python
from litestar import Litestar
from sqlalchemy.ext.asyncio import create_async_engine
from litestar_redline import RedlinePlugin

engine = create_async_engine("sqlite+aiosqlite:///app.db")

app = Litestar(
    route_handlers=[...],
    plugins=[RedlinePlugin(engine=engine, settings={"access": "local"})],  # localhost only
)
# UI at http://localhost:8000/_redline/
```

With no access settings the FastAPI, Flask and Litestar UIs answer only to loopback clients
and log a warning.

## What you get

- **Requests.** Every request with method, route template (`/orders/<int:pk>/`,
  `/orders/{order_id}`), view, status, duration, query count and issue badges. Filter,
  search, **live mode**, keyboard navigation and a **command palette** (`Ctrl-K`).
- **Request detail.** A waterfall of the span tree (middleware, view, SQL, cache, template,
  serializer, transaction, outbound HTTP, your own spans), SQL grouped by fingerprint with
  the project `file:line` that ran it (click to **open in your editor**), redacted headers
  and bodies, **logs** emitted during the request, and the traceback of an unhandled
  exception with the **local variables** of each frame. **Copy as cURL or HTTPie**,
  **replay**, **compare** two requests, or download an offline **snapshot**.
- **Issues.** N+1, duplicate, slow and over-budget queries, plus event-loop blocking, long
  transactions, HTTP calls inside transactions, huge and unbounded results, `len(qs)` where
  `.exists()` belongs, slow serializers, and slow, duplicated or retried external calls,
  deduplicated across requests.
- **The fix.** The Django suggester maps the repeated SQL to your models and writes the
  patch:

  ```diff
  --- a/shop/views.py
  +++ b/shop/views.py
  @@ -32,5 +32,5 @@
   def order_list(request):
  -    orders = Order.objects.select_related("customer").order_by("-id")[:30]
  +    orders = Order.objects.select_related("customer").prefetch_related("items").order_by("-id")[:30]
  ```

  The SQLAlchemy suggester does the same with `selectinload(...)` and `joinedload(...)`.
  Over GraphQL (Strawberry, Graphene) the N+1 names the resolver field.
- **EXPLAIN and the Database page.** On any captured `SELECT`, with plan analysis and
  `CREATE INDEX` suggestions. The Database page searches SQL across all requests, ranks index
  suggestions for the whole workload, and lists unused indexes.
- **Load testing.** `redline load scenario.toml --steps 10:100:10`: an open-model generator
  in separate processes, corrected for coordinated omission, that finds the **knee** and
  shows which query or endpoint got slower at which step. `--fail-p95-ms` makes it a CI
  gate.
- **History.** Hourly rollups kept for 90 days, stamped with your release. Compare two
  releases: a verdict per endpoint, and the issues that are new or resolved.
- **Flamegraphs.** Add `?_redline=profile` to a request; pyinstrument profiles it,
  async-safe, and the UI draws it. Download as speedscope JSON.
- **Endpoints, Cache and External.** p50, p95 and p99 per route, cache hit rates and
  stampedes, and outbound-call latency, duplicates and retries.
- **A static N+1 scan** of templates and DRF serializers, for CI.
- **A floating badge and `Server-Timing`** in debug mode (`badge` and `server_timing` default
  to `"auto"`), off in the `production` preset.
- **Your own spans.** `@redline_core.span("pricing.compute")`, or `with` and `async with`.

<table>
  <tr>
    <td width="50%">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screenshots/fastapi-dark-requests.png">
        <img alt="The requests list" src="docs/assets/screenshots/fastapi-light-requests.png">
      </picture>
      <p align="center"><b>Requests</b>: live list with filters and issue badges</p>
    </td>
    <td width="50%">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screenshots/fastapi-dark-detail-timeline.png">
        <img alt="A request's span timeline" src="docs/assets/screenshots/fastapi-light-detail-timeline.png">
      </picture>
      <p align="center"><b>Timeline</b>: the span tree of one request</p>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screenshots/django-explain-dark.png">
        <img alt="EXPLAIN drawer with findings and an index suggestion" src="docs/assets/screenshots/django-explain-light.png">
      </picture>
      <p align="center"><b>EXPLAIN</b>: plan, findings and suggested indexes</p>
    </td>
    <td width="50%">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screenshots/django-flamegraph-dark.png">
        <img alt="A flamegraph of one request" src="docs/assets/screenshots/django-flamegraph-light.png">
      </picture>
      <p align="center"><b>Flamegraph</b>: opt-in, per request</p>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screenshots/fastapi-dark-endpoints.png">
        <img alt="Endpoint aggregates" src="docs/assets/screenshots/fastapi-light-endpoints.png">
      </picture>
      <p align="center"><b>Endpoints</b>: percentiles, queries and trend per route</p>
    </td>
    <td width="50%">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screenshots/django-redaction-dark.png">
        <img alt="Redacted headers and body" src="docs/assets/screenshots/django-redaction-light.png">
      </picture>
      <p align="center"><b>Redaction</b>: secrets masked before they reach disk</p>
    </td>
  </tr>
</table>

## Load testing in a minute

```bash
pip install "redline-core[load] @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"

redline load init --from-traces -o scenario.toml      # from the traffic Redline has seen
redline load scenario.toml --steps 10:100:10 --step-duration 20s
```

```text
knee: step 8 – error rate 3.1% > 1%
max ok rps: 69.8
verdict: saturated
```

Then open the run in the UI to see the chart and the **bottlenecks** (`SELECT ... FROM
shop_order ...: 3 ms -> 410 ms`). Run it against environments you own, never production.
[Load testing guide](https://pythondeveloper6.github.io/redline/load-testing/).

## Configuration

Everything is optional. Settings are layered, last one wins: built-in defaults, the
adapter's defaults, a **preset** (`dev`, `staging`, `production`), a `redline.toml` (or
`[tool.redline]`), `REDLINE_*` environment variables, then your framework's own settings. `redline config` prints the
result and where each value came from.

```python
# Django: settings.py
REDLINE = {
    "PRESET": "staging",
    "SAMPLE_RATE": 0.25,
    "SLOW_QUERY_MS": 50,
    "IGNORE_PATHS": ["/static/", "/media/", "/favicon.ico", "^/health"],  # "^" = regex
    "PERMISSION": "myproject.redline.can_view",   # Django: who may open the UI
}
```

```python
# FastAPI
install(app, engine=engine, settings={"preset": "staging", "slow_query_ms": 50})
```

Keys are case-insensitive and an unknown key is an error with a "did you mean" hint. A list
you set **replaces** the default list (setting `ignore_paths` drops `/static/` unless you
include it). See [Configuration](https://pythondeveloper6.github.io/redline/configuration/)
and the [settings reference](https://pythondeveloper6.github.io/redline/settings/).

## Security

- **Access.** One policy for every framework: `local` (loopback only, served in debug mode until `access` is set; Django: superusers),
  `token`, `basic`, the framework's own hook, or your own check, plus IP allow-lists,
  trusted proxies, a read-only mode and per-feature switches. Behind a reverse proxy
  use a token and `trusted_proxies`; a loopback peer carrying forwarding headers is refused. See
  [Access control](https://pythondeveloper6.github.io/redline/access-control/).
- **CSRF.** Every non-GET call is protected: a shared `Origin` / `Sec-Fetch-Site` check
  (cross-site and same-site are refused) on all four adapters, plus Django's own CSRF check
  on Django.
- **Redaction** is on by default: `Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key` and
  password, token, secret, card and similar keys in query strings, bodies, SQL parameters,
  log messages and exception locals are masked **before** anything is stored. It is
  **key-based**: a secret in a URL path, personal data under ordinary keys, or free text in
  an exception message is **not** caught. Read
  [Security](https://pythondeveloper6.github.io/redline/security/#what-redaction-does-not-catch).
- **Data on disk.** `.redline/redline.db` is not encrypted. Keep it out of Git, keep
  retention short, and keep the UI off the public internet.
- **Load from the UI** is off by default. It sends only paths, to the `base_url` you set or
  to a loopback / `allowed_hosts` address, and is capped at one hour, 5 000 rps and 200
  steps.
- Requests to Redline's own URLs are never recorded; its storage is never a Django
  connection or one of your engines.

Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

## Limitations

Honest, and not exhaustive. The [FAQ](https://pythondeveloper6.github.io/redline/faq/) has
more.

- **Alpha.** The API, settings and storage schema may change. The store migrates itself in
  place, but there is no deprecation cycle yet.
- **Overhead is measured on one machine.** On v0.2 about 0.04 to 0.09 ms per request plus 28
  to 51 µs per query on an Apple M2 Pro (v0.1: 0.05 to 0.06 ms and 22 to 44 µs)
  ([numbers](https://pythondeveloper6.github.io/redline/performance/#measured-numbers)).
  `sample_rate` reduces analysis and disk work, **not** the per-query recording cost.
- **Not hardened for production fleets.** Single-node SQLite. Multi-process use shares one
  file; the store is fork-safe (gunicorn `--preload` works), but busy multi-worker
  deployments have not been load-tested.
- **No distributed tracing, no alerting.** The load tester is HTTP-only, with no browsers,
  WebSockets or distributed generators.
- **Aggregates describe stored traces.** Sampling plus always-keep-slow biases percentiles
  upward.
- **Fix suggestions only for the Django ORM and SQLAlchemy.** WebSockets are not recorded.
- **EXPLAIN index suggestions, and the workload index advisor, are hypotheses.** Check them
  on a copy.

## Roadmap

- **v0.2 (done).** Flask and Litestar, ten new detectors, the Database page, a static N+1
  scan, the load tester, release history, configuration presets and unified access control.
- **v0.3 Compare and CI.** A pytest plugin (`@pytest.mark.redline(max_queries=...,
  no_n_plus_one=True)`), `redline ci` with JUnit and a Markdown PR comment, flamegraph
  diffs, OTLP.
- **v1.0.** Hardened production mode with a Postgres store, Celery and RQ stitching, memory
  per request, fault injection, HypoPG index what-if, optional AI diagnosis.

Details: [Roadmap](https://pythondeveloper6.github.io/redline/roadmap/).

## Documentation

The documentation is a MkDocs Material site, published at
**<https://pythondeveloper6.github.io/redline/>**. The source is in [`docs/`](docs/index.md);
to build it locally:

```bash
uv run --with mkdocs-material mkdocs serve          # http://127.0.0.1:8000
uv run --with mkdocs-material mkdocs build --strict
```

| | |
|---|---|
| Start | [Getting started](docs/getting-started.md) · [How it works](docs/how-it-works.md) · [The UI](docs/the-ui.md) |
| Analysis | [Detectors](docs/detectors.md) · [N+1](docs/n-plus-one.md) · [EXPLAIN](docs/explain.md) · [Database](docs/database.md) · [Flamegraphs](docs/flamegraphs.md) · [Static scan](docs/static-scan.md) |
| Load and history | [Load testing](docs/load-testing.md) · [History](docs/history.md) |
| Integrations | [Django](docs/django.md) · [FastAPI](docs/fastapi.md) · [Flask](docs/flask.md) · [Litestar](docs/litestar.md) · [GraphQL](docs/graphql.md) · [Integrations](docs/integrations.md) · [HTTP API](docs/http-api.md) |
| Operations | [Configuration](docs/configuration.md) · [Access control](docs/access-control.md) · [Production](docs/production.md) · [Security](docs/security.md) · [Storage](docs/storage.md) · [CLI](docs/cli.md) |

## Try the demos

Demo apps with seeded data and deliberately broken endpoints (an N+1, a missing index, a
duplicate query, a slow endpoint):

```bash
git clone https://github.com/Pythondeveloper6/redline
cd redline
uv sync --all-packages

# Django: http://127.0.0.1:8000/  then  /redline/  (admin / admin, demo only)
uv run python examples/django/manage.py migrate
uv run python examples/django/manage.py seed
uv run python examples/django/manage.py runserver

# FastAPI: http://127.0.0.1:8000/_redline/
cd examples/fastapi
uv run --project ../.. python seed.py
uv run --project ../.. uvicorn shop.main:app --port 8000
```

There is also a Flask demo in [`examples/flask`](examples/flask).

## Repository layout

| Directory | Distribution | Depends on |
|---|---|---|
| `packages/redline-core` | `redline-core` | nothing (ships the built UI) |
| `packages/django-redline` | `django-redline` | core (exact pin), Django |
| `packages/fastapi-redline` | `fastapi-redline` | core (exact pin), FastAPI, SQLAlchemy |
| `packages/flask-redline` | `flask-redline` | core (exact pin), Flask, SQLAlchemy |
| `packages/litestar-redline` | `litestar-redline` | core (exact pin), Litestar, SQLAlchemy |
| `frontend/` | (source of the UI) | React 19, Vite, TypeScript, Tailwind |
| `examples/` | demo apps | |

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md); security issues go
privately to [SECURITY.md](SECURITY.md).

```bash
uv sync --all-packages
(cd packages/redline-core && uv run pytest)
(cd packages/django-redline && uv run pytest)
(cd packages/fastapi-redline && uv run pytest)
(cd packages/flask-redline && uv run pytest)
(cd packages/litestar-redline && uv run pytest)
uv run ruff check . && uv run ruff format --check .
```

## Licence

Redline is open source under the [MIT licence](LICENSE). Copyright &copy; 2026 Mahmoud
Ahmed.
