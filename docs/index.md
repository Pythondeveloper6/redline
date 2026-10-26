---
title: Redline
hide:
  - navigation
  - toc
---

<div class="rl-hero" markdown>

# See inside every request

<p class="rl-tagline">A self-hosted request profiler and backend analyzer for Django and FastAPI.
Every request becomes a trace: a span timeline, the SQL it ran, N+1 queries with a
ready-to-apply fix diff, one-click EXPLAIN and optional flamegraphs, in a fast web UI
served from your own app.</p>

[Get started](getting-started.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/Pythondeveloper6/redline){ .md-button }

</div>

![The N+1 issue of the Django demo shop in Redline: 30 queries from one line, with a prefetch_related diff](assets/screenshots/django-n-plus-one-light.png){ .rl-shot .rl-only-light }
![The N+1 issue of the Django demo shop in Redline, dark theme](assets/screenshots/django-n-plus-one-dark.png){ .rl-shot .rl-only-dark }

!!! warning "Alpha: `0.1.0a1`, private, not published"

    Redline is working and tested, but the API may still move and **nothing is published to
    PyPI**: install it from the Git repository (see [Getting started](getting-started.md#install)).
    See the [FAQ](faq.md#how-finished-is-this) for an honest account of what is and is not
    done. Use it in development and on internal tools first, and read
    [Production](production.md) before pointing it at real traffic.

## Sixty seconds

=== "Django"

    ```bash
    pip install \
      "django-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/django-redline" \
      "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"
    ```

    ```python
    # settings.py
    INSTALLED_APPS = [..., "django_redline"]

    MIDDLEWARE = [
        "django.middleware.security.SecurityMiddleware",
        "django_redline.middleware.RedlineMiddleware",   # as high as possible
        ...,
    ]
    ```

    ```python
    # urls.py
    from django.urls import include, path

    urlpatterns = [
        ...,
        path("redline/", include("django_redline.urls")),
    ]
    ```

    Run your app, make a few requests, and open `/redline/` as a staff user.

=== "FastAPI"

    ```bash
    pip install \
      "fastapi-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/fastapi-redline" \
      "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"
    ```

    ```python
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import create_async_engine
    from fastapi_redline import install

    app = FastAPI()
    engine = create_async_engine("sqlite+aiosqlite:///app.db")

    install(app, engine=engine)   # UI at http://localhost:8000/_redline/
    ```

That is the whole setup. Traces go to their own SQLite file (`.redline/redline.db`),
never to your application database.

Both lines are needed because Redline is not on PyPI: the adapter pins `redline-core`, and
pip cannot fetch it from anywhere unless you name it too. [Details](getting-started.md#install).

## What you get

<div class="grid cards" markdown>

-   :material-timeline-clock-outline:{ .lg .middle } **A trace for every request**

    ---

    Method, route template, view, status, headers, redacted bodies and a waterfall of
    spans: middleware, view, SQL, cache, template, outbound HTTP and your own code.

    [:octicons-arrow-right-24: How it works](how-it-works.md)

-   :material-database-search-outline:{ .lg .middle } **N+1 with the fix attached**

    ---

    The repeated query is mapped back to your models and the queryset in your source,
    and written out as a unified diff: `prefetch_related`, `select_related`,
    `selectinload`, `joinedload`.

    [:octicons-arrow-right-24: N+1 detection](n-plus-one.md)

-   :material-magnify-scan:{ .lg .middle } **One-click EXPLAIN**

    ---

    On any captured `SELECT`, inside a rolled-back, read-only transaction. Plans are
    analysed for scans, spills and missing indexes, with a `CREATE INDEX` suggestion.

    [:octicons-arrow-right-24: EXPLAIN](explain.md)

-   :material-fire:{ .lg .middle } **Opt-in flamegraphs**

    ---

    Add `?_redline=profile` to a URL. pyinstrument samples that one request, async-safe,
    and the UI draws it. Export speedscope JSON.

    [:octicons-arrow-right-24: Flamegraphs](flamegraphs.md)

-   :material-chart-line:{ .lg .middle } **Endpoints and issues**

    ---

    p50, p95 and p99 per route, query counts, error rates and sparklines; every problem
    deduplicated across requests with first seen, last seen and a count.

    [:octicons-arrow-right-24: Issues and endpoints](issues-and-endpoints.md)

-   :material-shield-lock-outline:{ .lg .middle } **Redacted by default**

    ---

    `Authorization`, cookies and password, token and card keys are masked before anything
    is stored. The UI is staff-only (Django) or loopback-only (FastAPI) unless you say
    otherwise.

    [:octicons-arrow-right-24: Security](security.md)

-   :material-flash-outline:{ .lg .middle } **Async-correct**

    ---

    State lives in `contextvars`: concurrent requests on one event loop, `sync_to_async`
    and Starlette's threadpool all attribute to the right request.

    [:octicons-arrow-right-24: How it works](how-it-works.md#why-contextvars)

-   :material-puzzle-outline:{ .lg .middle } **Extensible**

    ---

    Plug in your own `FixSuggester` for any ORM, or a custom `Store`. Time your own code
    with `redline_core.span`.

    [:octicons-arrow-right-24: Extending](extending.md) · [Custom spans](custom-spans.md)

</div>

## A closer look

=== "Requests"

    ![Requests list](assets/screenshots/fastapi-light-requests.png){ .rl-shot .rl-only-light }
    ![Requests list](assets/screenshots/fastapi-dark-requests.png){ .rl-shot .rl-only-dark }

=== "Timeline"

    ![Request timeline](assets/screenshots/fastapi-light-detail-timeline.png){ .rl-shot .rl-only-light }
    ![Request timeline](assets/screenshots/fastapi-dark-detail-timeline.png){ .rl-shot .rl-only-dark }

=== "Issues"

    ![Deduplicated issues](assets/screenshots/django-issues-light.png){ .rl-shot .rl-only-light }
    ![Deduplicated issues](assets/screenshots/django-issues-dark.png){ .rl-shot .rl-only-dark }

=== "EXPLAIN"

    ![EXPLAIN drawer](assets/screenshots/django-explain-light.png){ .rl-shot .rl-only-light }
    ![EXPLAIN drawer](assets/screenshots/django-explain-dark.png){ .rl-shot .rl-only-dark }

=== "Flamegraph"

    ![Flamegraph](assets/screenshots/django-flamegraph-light.png){ .rl-shot .rl-only-light }
    ![Flamegraph](assets/screenshots/django-flamegraph-dark.png){ .rl-shot .rl-only-dark }

=== "Endpoints"

    ![Endpoint aggregates](assets/screenshots/fastapi-light-endpoints.png){ .rl-shot .rl-only-light }
    ![Endpoint aggregates](assets/screenshots/fastapi-dark-endpoints.png){ .rl-shot .rl-only-dark }

=== "Redaction"

    ![Redacted headers and body](assets/screenshots/django-redaction-light.png){ .rl-shot .rl-only-light }
    ![Redacted headers and body](assets/screenshots/django-redaction-dark.png){ .rl-shot .rl-only-dark }

=== "FastAPI N+1"

    ![SQLAlchemy N+1 with a selectinload diff](assets/screenshots/fastapi-light-detail-issues.png){ .rl-shot .rl-only-light }
    ![SQLAlchemy N+1 with a selectinload diff](assets/screenshots/fastapi-dark-detail-issues.png){ .rl-shot .rl-only-dark }

## Why another profiler

Request profilers split into two camps. In-page toolbars show one request, in
development, and forget it. Hosted APMs see production but not your source lines, and
they are another vendor. The self-hosted middle ground is thin: [django-silk](https://github.com/jazzband/django-silk)
is Django-only and has no async story, and nothing comparable exists for FastAPI.

Redline is built for that middle: a **persistent** record of recent requests, in your
own process and your own disk, that knows enough about your ORM to say *which line to
change*. It is not trying to replace a hosted APM for fleet-wide production monitoring
(see the [comparison](#how-it-compares) and the [FAQ](faq.md)).

## How it compares

| | **Redline** | django-silk | django-debug-toolbar | Kolo | Sentry / Logfire | Locust / k6 |
|---|---|---|---|---|---|---|
| Django | Yes | Yes | Yes | Yes | Yes | n/a (black box) |
| FastAPI / SQLAlchemy | Yes | No | No | Not its focus | Yes | n/a |
| Async-correct recording | Yes (`contextvars`) | No async support | Partial (async views supported in recent versions) | n/a | Yes | n/a |
| Persists traces across requests | Yes, own SQLite file | Yes, in your app DB | History panel only | Local trace store | Hosted | Results only |
| N+1 detection | Yes, grouped and deduplicated | Manual (count queries) | Query counts and "similar" markers | Shows repeated queries | Yes (performance issues) | No |
| Fix suggestion as a diff | **Yes** (Django and SQLAlchemy) | No | No | No | No (generic advice) | No |
| EXPLAIN | Yes, safe by default | No | Yes (per query, in page) | No | No | No |
| Plan analysis + index suggestion | Yes | No | No | No | No | No |
| Flamegraphs | Opt-in, per request | cProfile call tree | Optional profiling panel | Call traces | Profiling products | No |
| Secrets redacted on write | Yes, by default | Stores bodies and headers as captured | n/a (not persisted) | Local only | Scrubbing is configurable | n/a |
| Runs in production | Possible, with care ([sampling](production.md)) | Heavy; not advised | No | No | **Built for it** | Against staging |
| Distributed tracing across services | **No** | No | No | No | **Yes** | n/a |
| Load testing | **Not yet** ([v0.2](roadmap.md)) | No | No | No | No | **Yes** |
| Hosted, zero ops | No | No | No | Editor extension | Yes | Yes |

Read this table with its omissions in mind. If you need cross-service traces, alerting,
or fleet dashboards, use Sentry or Logfire (Redline can sit beside them). If you need
a load generator today, use Locust or k6: Redline's stress tester is planned, not
built. Where this table says "No" for another tool it means "not as a built-in
feature", and tools change: check theirs.

## Requirements

| | Python | Framework | Other |
|---|---|---|---|
| `redline-core` | 3.10+ | none | no runtime dependencies |
| `django-redline` | 3.12+ | Django 5.2, 6.0 or 6.1 | |
| `fastapi-redline` | 3.10+ | FastAPI 0.110+ | SQLAlchemy 2.x |

Flamegraphs need the `profile` extra (pyinstrument). Outbound `httpx` spans need httpx.
There is no Node.js requirement: the web UI ships prebuilt inside `redline-core`.

## Map of the documentation

| Page | What is in it |
|---|---|
| [Getting started](getting-started.md) | Install, mount, first look, for Django and FastAPI |
| [How it works](how-it-works.md) | Recording, `contextvars`, sampling, storage and the overhead model |
| [The UI](the-ui.md) | A tour of every page, filter and shortcut |
| [N+1 detection](n-plus-one.md) | Detection rules, thresholds, fix suggestions with real diffs, confidence |
| [EXPLAIN](explain.md) | Vendors, safety, findings, index suggestions and their limits |
| [Flamegraphs](flamegraphs.md) | Opt-in profiling, modes, speedscope export |
| [Custom spans](custom-spans.md) | Timing your own code with `redline_core.span` |
| [Issues and endpoints](issues-and-endpoints.md) | Aggregates, percentiles, issue deduplication |
| [Django](django.md) | Middleware, URLs, settings, permission, CSRF, hooks, checks, commands, DRF |
| [FastAPI](fastapi.md) | `install()`, engines, httpx, auth dependencies, CSRF, lifespan |
| [The HTTP API](http-api.md) | Every endpoint with request and response examples |
| [Extending](extending.md) | `FixSuggester` and the `Store` protocol |
| [Settings reference](settings.md) | Every setting, its type, default and effect |
| [Production](production.md) | Sampling, retention, overhead, what not to do |
| [Security](security.md) | Redaction, authentication, exposure, data on disk |
| [Storage](storage.md) | The SQLite store, the writer thread, retention |
| [Performance and overhead](performance.md) | The cost model and the measured overhead |
| [Roadmap](roadmap.md) | v0.2 Stress, v0.3 Compare and CI, v1.0 |
| [FAQ](faq.md) | How finished it is, how it differs from other tools |

## Licence

Redline is open source under the
[MIT licence](https://github.com/Pythondeveloper6/redline/blob/main/LICENSE),
copyright &copy; 2026 Mahmoud Ahmed.
