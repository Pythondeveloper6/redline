<div align="center">

<a href="https://github.com/Pythondeveloper6/redline">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img alt="Redline" src="docs/assets/logo-light.svg" height="64">
  </picture>
</a>

**A request profiler and backend analyzer for Django and FastAPI.**<br>
See inside every request: N+1 queries with a fix diff, one-click EXPLAIN, flamegraphs.

[![Python](https://img.shields.io/badge/python-3.10%2B-e5392f?logo=python&logoColor=white)](https://github.com/Pythondeveloper6/redline)
[![Django](https://img.shields.io/badge/django-5.2%20%7C%206.0%20%7C%206.1-0c4b33?logo=django&logoColor=white)](https://github.com/Pythondeveloper6/redline/tree/main/packages/django-redline)
[![FastAPI](https://img.shields.io/badge/fastapi-0.110%2B-009688?logo=fastapi&logoColor=white)](https://github.com/Pythondeveloper6/redline/tree/main/packages/fastapi-redline)
[![CI](https://github.com/Pythondeveloper6/redline/actions/workflows/ci.yml/badge.svg)](https://github.com/Pythondeveloper6/redline/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-e5392f?logo=opensourceinitiative&logoColor=white)](https://github.com/Pythondeveloper6/redline/blob/main/LICENSE)

[**Documentation**](#documentation) ·
[Getting started](docs/getting-started.md) ·
[Changelog](CHANGELOG.md) ·
[FAQ](docs/faq.md)

</div>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/screenshots/django-n-plus-one-dark.png">
  <img alt="Redline showing an N+1 on order items in the Django demo shop: 30 queries from one line, with a prefetch_related diff" src="docs/assets/screenshots/django-n-plus-one-light.png">
</picture>

> **Alpha (`0.1.0a1`), private, not published.** Working and tested, but the API may still
> move, and nothing is on PyPI or GitHub Pages: you install from Git and build the docs
> locally. See the [FAQ](docs/faq.md#how-finished-is-this) for an honest account of what
> is and is not done. Use it in development and on internal tools first, and read
> [Production](docs/production.md) before pointing it at real traffic.

## Why

Request profilers split into two camps. In-page toolbars show one request, in development,
and forget it. Hosted APMs see production but not your source lines. The self-hosted middle
is thin: django-silk is Django-only and has no async story, and FastAPI has nothing
comparable. Load testers only see an application from the outside.

Redline is built for that middle. It keeps a **persistent record** of recent requests, in
your own process and on your own disk, and it knows enough about your ORM to say **which
line to change**.

- **Async-correct.** State lives in `contextvars`, so concurrent requests, `sync_to_async`
  and Starlette's threadpool all attribute to the right request.
- **Fix, not just a finding.** An N+1 is mapped back to your models and the queryset in
  your source, and written out as a unified diff.
- **Safe by default.** Secrets are redacted before storage. EXPLAIN runs only on recorded
  `SELECT`s, in a rolled-back transaction. The UI is staff-only (Django) or loopback-only
  (FastAPI) until you say otherwise.
- **Out of your way.** Its own SQLite file, never your database. A background writer, so no
  disk I/O on the request path. No runtime dependencies in the core.

## How it compares

| | **Redline** | django-silk | django-debug-toolbar | Kolo | Sentry / Logfire | Locust / k6 |
|---|---|---|---|---|---|---|
| Django / FastAPI | Both | Django | Django | Django | Both | n/a |
| Async-correct | Yes | No async support | Partial | n/a | Yes | n/a |
| Persists across requests | Own SQLite file | In your app DB | History panel | Local | Hosted | Results only |
| N+1 grouped and deduplicated | Yes | No | Markers | Repeats shown | Yes | No |
| Fix as a diff | **Yes** | No | No | No | No | No |
| EXPLAIN + index suggestion | Yes | No | EXPLAIN only | No | No | No |
| Flamegraphs | Opt-in | cProfile tree | Optional panel | Call traces | Profiling products | No |
| Secrets redacted on write | Yes | No | n/a | Local | Configurable | n/a |
| Built for production fleets | **No** | No | No | No | **Yes** | n/a |
| Distributed tracing | **No** | No | No | No | **Yes** | n/a |
| Load testing | **Not yet** | No | No | No | No | **Yes** |

Where a cell says "No" for another tool it means "not as a built-in feature", and tools
change: check theirs. Redline does **not** replace a hosted APM for fleet-wide monitoring or
a load generator. Load testing is planned for v0.2. More in the
[docs](docs/index.md#how-it-compares).

## Install

Redline is not on PyPI. Install from Git, and **name `redline-core` in the same command**:
each adapter pins the exact core it was tested with (`redline-core==0.1.0a1`), and pip
cannot find that on PyPI, so without the second requirement the install fails.

```bash
# Django
pip install \
  "django-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/django-redline" \
  "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"

# FastAPI
pip install \
  "fastapi-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/fastapi-redline" \
  "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"
```

Installing an adapter alone fails with `Could not find a version that satisfies the
requirement redline-core==0.1.0a1`. Do not "fix" that by installing `redline-core` from
PyPI: this project does not own the name there.

For flamegraphs, ask for the extra on the core:
`"redline-core[profile] @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"`.

To hack on it or run the demos, clone the repository and run `uv sync --all-packages`.

Python 3.10+ for the core and FastAPI adapter, 3.12+ for Django; Django 5.2, 6.0 or 6.1;
FastAPI 0.110+ with SQLAlchemy 2.x.

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

Run your app, make some requests, and open **`/redline/`** as a staff user.
`python manage.py check` tells you if anything is miswired.

### FastAPI

```python
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from fastapi_redline import install

app = FastAPI()
engine = create_async_engine("sqlite+aiosqlite:///app.db")

install(app, engine=engine)      # UI at http://localhost:8000/_redline/
```

With no `dependencies` the UI answers only to loopback clients and logs a warning.

## What you get

- **Requests.** Every request with method, route template (`/orders/<int:pk>/`,
  `/orders/{order_id}`), view, status, duration, query count and issue badges. Filter,
  search, live polling, keyboard navigation.
- **Request detail.** A waterfall of the span tree (middleware, view, SQL, cache, template,
  outbound HTTP, your own spans), SQL grouped by fingerprint with the project `file:line`
  that ran it, redacted headers and bodies, and the traceback of an unhandled exception.
- **Issues.** N+1, duplicate, slow and over-budget queries, deduplicated across requests,
  with first seen, last seen and a count.
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
  Every suggestion carries a **confidence** (`high`, `medium`, `low`); when the queryset
  cannot be located you get a code hint instead of a diff.
- **EXPLAIN.** On any captured `SELECT`, with plan analysis (scans, sort and hash spills,
  nested loops, temp sorts) and `CREATE INDEX` suggestions that know when a B-tree cannot
  help. PostgreSQL, MySQL and SQLite. `ANALYZE` is opt-in because it executes the query.
- **Flamegraphs.** Add `?_redline=profile` (or `X-Redline-Profile: 1`) to a request;
  pyinstrument profiles it, async-safe, and the UI draws it. Download as speedscope JSON.
- **Endpoints.** p50, p95 and p99 per route, query counts, error rate and a trend
  sparkline.
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

## Configuration

Everything is optional. Keys are case-insensitive, and an unknown key is an error with a
"did you mean" hint.

```python
# Django: settings.py
REDLINE = {
    "SAMPLE_RATE": 1.0,               # default 1.0 with DEBUG=True, 0.1 with DEBUG=False
    "ALWAYS_KEEP_SLOWER_THAN_MS": 500,
    "ALWAYS_KEEP_ERRORS": True,
    "IGNORE_PATHS": ["/static/", "/media/", "/favicon.ico", "^/health"],  # "^" = regex
    "STORAGE_PATH": str(BASE_DIR / ".redline" / "redline.db"),            # the default
    "N_PLUS_ONE_THRESHOLD": 5,
    "SLOW_QUERY_MS": 100,
    "TOO_MANY_QUERIES": 50,
    "PROFILE": "opt_in",              # off | opt_in | sampled | all
    "EXPLAIN_ANALYZE": False,         # ANALYZE executes the query: off by default
    "MAX_ROWS": 10_000, "MAX_AGE_DAYS": 7,
    "PERMISSION": "myproject.redline.can_view",   # Django-only: who may open the UI
}
```

```python
# FastAPI
install(app, engine=engine, settings={"sample_rate": 0.1, "slow_query_ms": 50},
        dependencies=[Depends(require_admin)])
```

A list you set **replaces** the default list (setting `ignore_paths` drops `/static/`
unless you include it). The full table is the [settings reference](docs/settings.md).

## Security

- **Access.** Django: active staff users by default, or your `PERMISSION` callable
  (it fails closed). FastAPI: loopback clients only by default, or your `dependencies`.
  Behind a reverse proxy "loopback" may mean everyone: pass real dependencies.
- **CSRF.** The mutating calls (`DELETE` traces, `POST api/explain`) are protected: Django's
  own CSRF check on every API request, and an `Origin` / `Sec-Fetch-Site` guard on FastAPI.
- **Redaction** is on by default: `Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key` and
  password, token, secret, card and similar keys in query strings, bodies and SQL
  parameters are masked **before** anything is stored. It is **key-based**: a secret in a
  URL path, personal data under ordinary keys, or free text in an exception message is
  **not** caught. Read [Security](docs/security.md#what-redaction-does-not-catch).
- **Data on disk.** `.redline/redline.db` is not encrypted. Keep it out of Git, keep
  retention short, and keep the UI off the public internet.
- Requests to Redline's own URLs are never recorded; its storage is never a Django
  connection or one of your engines.

Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

## Limitations

Honest, and not exhaustive. The [FAQ](docs/faq.md) has more.

- **Alpha.** The API, settings and storage schema may change. No upgrade path yet.
- **Overhead is not benchmarked.** The [cost model](docs/performance.md) is documented;
  measured numbers are not. `sample_rate` reduces analysis and disk work, **not** the
  per-query recording cost.
- **Not hardened for production fleets.** Single-node SQLite. Multi-process use shares one
  file and is untested. With FastAPI do not use `gunicorn --preload`
  ([why](docs/production.md#process-models)).
- **No load testing yet, no distributed tracing, no alerting.**
- **Aggregates describe stored traces.** Sampling plus always-keep-slow biases percentiles
  upward.
- **FastAPI and Django only**, and fix suggestions only for the Django ORM and SQLAlchemy.
  WebSockets are not recorded.
- **EXPLAIN index suggestions are hypotheses** from one query's plan.

## Roadmap

- **v0.2 Stress.** A `redline load` CLI and UI page: open-model arrival-rate load
  generation in separate processes, with coordinated-omission-corrected histograms and a
  step ramp that finds the knee. Requests carry `X-Redline-Run`, so in-app traces show
  **which query got slow at which load**.
- **v0.3 Compare and CI.** Baselines tagged by git SHA with per-endpoint and flamegraph
  diffs, a pytest plugin (`@pytest.mark.redline(max_queries=..., no_n_plus_one=True)`) and
  `redline ci` with JUnit and a Markdown PR comment.
- **v1.0.** Hardened production mode with a Postgres store, Celery and RQ stitching, memory
  per request, fault injection, HypoPG index what-if, OTLP, optional AI diagnosis, and
  folding `django-signal-tracer` in.

Details: [Roadmap](docs/roadmap.md).

## Documentation

The documentation is a MkDocs Material site in [`docs/`](docs/index.md). It is **not
deployed** (there is no GitHub Pages site yet), so build it locally:

```bash
uv run --with mkdocs-material mkdocs serve          # http://127.0.0.1:8000
uv run --with mkdocs-material mkdocs build --strict
```

Or read the Markdown directly:

| | |
|---|---|
| Start | [Getting started](docs/getting-started.md) · [How it works](docs/how-it-works.md) · [The UI](docs/the-ui.md) |
| Features | [N+1 detection](docs/n-plus-one.md) · [EXPLAIN](docs/explain.md) · [Flamegraphs](docs/flamegraphs.md) · [Custom spans](docs/custom-spans.md) · [Issues and endpoints](docs/issues-and-endpoints.md) |
| Integrations | [Django](docs/django.md) · [FastAPI](docs/fastapi.md) · [HTTP API](docs/http-api.md) · [Extending](docs/extending.md) |
| Operations | [Settings](docs/settings.md) · [Production](docs/production.md) · [Security](docs/security.md) · [Storage](docs/storage.md) · [Performance](docs/performance.md) |

## Try the demos

Two demo shops with seeded data and deliberately broken endpoints (an N+1, a missing index,
a duplicate query, a slow endpoint):

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

## Repository layout

| Directory | Distribution | Depends on |
|---|---|---|
| `packages/redline-core` | `redline-core` | nothing (ships the built UI) |
| `packages/django-redline` | `django-redline` | core (exact pin), Django |
| `packages/fastapi-redline` | `fastapi-redline` | core (exact pin), FastAPI, SQLAlchemy |
| `frontend/` | (source of the UI) | React 19, Vite, TypeScript, Tailwind |
| `examples/` | demo shops | |

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md); security issues go
privately to [SECURITY.md](SECURITY.md).

```bash
uv sync --all-packages
(cd packages/redline-core && uv run pytest)
(cd packages/django-redline && uv run pytest)
(cd packages/fastapi-redline && uv run pytest)
uv run ruff check . && uv run ruff format --check .
```

## Licence

Redline is open source under the [MIT licence](LICENSE). Copyright &copy; 2026 Mahmoud
Ahmed.
