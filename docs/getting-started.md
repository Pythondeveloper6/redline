# Getting started

[← Index](index.md)

This page takes a Django, FastAPI, Flask or Litestar project from nothing to a populated
Redline UI in a few minutes, then shows you how to confirm everything is wired up and what
to do next.

## Install

Redline is a core and one adapter per framework, released in lockstep:

| Distribution | Import name | Install it when |
|---|---|---|
| `redline-core` | `redline_core` | always (the engine, the JSON API, the bundled UI and the `redline` command) |
| `django-redline` | `django_redline` | you run Django (including Django Ninja) |
| `fastapi-redline` | `fastapi_redline` | you run FastAPI with SQLAlchemy |
| `flask-redline` | `flask_redline` | you run Flask, with or without Flask-SQLAlchemy |
| `litestar-redline` | `litestar_redline` | you run Litestar with SQLAlchemy |

Published prereleases are available on PyPI. Choose your framework's adapter; it
installs its matching core. The checkout may contain newer features than the published
version, so use a local workspace when testing unreleased changes.

=== "Django"

    ```bash
    pip install --pre django-redline
    ```

=== "FastAPI"

    ```bash
    pip install --pre fastapi-redline
    ```

=== "Flask"

    ```bash
    pip install --pre flask-redline
    ```

=== "Litestar"

    ```bash
    pip install --pre litestar-redline
    ```

Each adapter pins the exact core it was tested with. Pin both to the same release in
your deployment lock file; do not mix an unreleased adapter with an older core. Review the
[PyPI project](https://pypi.org/project/django-redline/) and release history when choosing
a published version. Alpha releases require deployment-specific validation before use
with real traffic; see [Production](production.md).

Add extras to the **core** requirement:

| Extra | Brings | For |
|---|---|---|
| `profile` | pyinstrument | [flamegraphs](flamegraphs.md) |
| `load` | httpx | the [load tester](load-testing.md) |
| `httpx`, `requests`, `redis`, `asyncpg`, `psycopg`, `strawberry`, `graphene` | the library at a compatible version | the matching [integration](integrations.md) |

```bash
pip install --pre "redline-core[profile,load]"
```

Use the core version pinned by your installed adapter when adding extras. `uv pip install`
accepts the same requirements. If installing directly from Git, install adapter and core
from the same reviewed commit in one command; pin that commit instead of a moving branch.

To work on Redline itself, or to try the demos, clone the repository instead:

```bash
git clone https://github.com/Pythondeveloper6/redline
cd redline
uv sync --all-packages        # every package, editable, plus the dev tools
```

### Supported versions

| | Supported |
|---|---|
| Python for `redline-core`, `fastapi-redline`, `flask-redline`, `litestar-redline` | 3.10, 3.11, 3.12, 3.13, 3.14 |
| Python for `django-redline` | 3.12, 3.13, 3.14 |
| Django | 5.2 LTS, 6.0, 6.1 (`>=5.2,<6.2`) |
| FastAPI | 0.110 and later, below 1.0 |
| Flask | 3.0 and later, below 4 |
| Litestar | 2.12 and later, below 3 |
| SQLAlchemy | 2.x |
| Databases for EXPLAIN | PostgreSQL, MySQL (and MariaDB), SQLite |

## Mount it

=== "Django"

    Three edits to your project.

    ```python
    # settings.py
    INSTALLED_APPS = [
        # ...
        "django_redline",
    ]

    MIDDLEWARE = [
        "django.middleware.security.SecurityMiddleware",
        # "django.middleware.gzip.GZipMiddleware",      # if you use it, keep it above Redline
        "django_redline.middleware.RedlineMiddleware",   # as high as possible
        "django.contrib.sessions.middleware.SessionMiddleware",
        # ...
    ]
    ```

    ```python
    # urls.py
    from django.urls import include, path

    urlpatterns = [
        # ...
        path("redline/", include("django_redline.urls")),
    ]
    ```

    There is no migration: Redline never touches your database. Run the system checks to
    confirm the wiring:

    ```bash
    python manage.py check
    ```

    A missing middleware, a missing URL mount, a `GZipMiddleware` placed wrongly and an
    unwritable storage directory are all reported with a hint. The complete list is in
    [Django: system checks](django.md#system-checks).

=== "FastAPI"

    One call, after the app is built and before it starts.

    ```python
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import create_async_engine

    from fastapi_redline import install

    app = FastAPI()
    engine = create_async_engine("sqlite+aiosqlite:///app.db")

    # ... your routers ...

    # access="local": localhost only (unset, Redline answers 404 outside debug mode)
    install(app, engine=engine, settings={"access": "local"})  # UI at /_redline/
    ```

    `engine` takes a sync `Engine`, an `AsyncEngine`, a list of them, or a dict of
    `{alias: engine}`. Omit it and Redline still records requests, but no SQL.
    See [FastAPI: engines](fastapi.md#engines).

    With no `dependencies` argument the UI answers **only to loopback clients that
    address it as `localhost`, `127.0.0.1` or `[::1]`** and logs a warning at startup. That is right for `uvicorn` on your laptop; read
    [FastAPI: access control](fastapi.md#access-control) before you deploy.

=== "Flask"

    One line, after the app is created (or `init_app` in a factory).

    ```python
    from flask import Flask
    from sqlalchemy import create_engine
    from flask_redline import Redline

    app = Flask(__name__)
    engine = create_engine("sqlite:///app.db")

    # ... your blueprints ...

    # access="local": localhost only (unset, Redline answers 404 outside debug mode)
    Redline(app, engine=engine, settings={"access": "local"})  # UI at /_redline/
    ```

    With **Flask-SQLAlchemy** omit the engine: `db = SQLAlchemy(app); Redline(app)` finds
    its engines, binds included. With no `permission` argument the UI answers only to
    loopback clients. See [Flask](flask.md).

=== "Litestar"

    One plugin.

    ```python
    from litestar import Litestar
    from sqlalchemy.ext.asyncio import create_async_engine
    from litestar_redline import RedlinePlugin

    engine = create_async_engine("sqlite+aiosqlite:///app.db")

    app = Litestar(
        route_handlers=[...],
        # access="local": localhost only (unset, Redline answers 404 outside debug mode)
        plugins=[RedlinePlugin(engine=engine, settings={"access": "local"})],  # UI at /_redline/
    )
    ```

    With no `guards` the UI answers only to loopback clients. See [Litestar](litestar.md).

## Look at it

Start your server, make a few requests, then open the UI.

=== "Django"

    ```bash
    python manage.py runserver
    ```

    Open `http://127.0.0.1:8000/redline/` while signed in as a **superuser**. An
    anonymous visitor is redirected to the admin login; a signed-in non-superuser gets a
    403. See [Django: access control](django.md#access-control).

=== "FastAPI"

    ```bash
    uvicorn app:app --reload
    ```

    Open `http://127.0.0.1:8000/_redline/` from the same machine.

=== "Flask"

    ```bash
    flask run
    ```

    Open `http://127.0.0.1:5000/_redline/` from the same machine.

=== "Litestar"

    ```bash
    litestar run --reload
    ```

    Open `http://127.0.0.1:8000/_redline/` from the same machine.

You will see the **Requests** page filling up, live. Open one. The tabs are **Timeline**,
**SQL**, **Issues**, **Profile**, **Logs**, **Exception** (when it raised) and
**Request / Response**; the [UI tour](the-ui.md) explains each. Press `Ctrl-K` (`Cmd-K` on a
Mac) for the command palette.

!!! tip "Nothing in the list?"

    Work through this short list.

    - **Is the request under an ignored path?** `/static/`, `/media/` and `/favicon.ico`
      are ignored by default, as is Redline's own mount.
    - **Django:** is `RedlineMiddleware` in `MIDDLEWARE`? `manage.py check` says so
      (`django_redline.W001`). Is `REDLINE["ENABLED"]` true?
    - **FastAPI:** was `install()` called on the app that uvicorn serves, before
      startup? Redline wraps the app it was installed on.
    - **Flask:** was `Redline(app)` (or `init_app`) called on the app you run? With
      Flask-SQLAlchemy, SQL appears after the first request.
    - **Litestar:** is `RedlinePlugin` in the `plugins` of the app you serve?
    - **Is the sample rate below 1?** In Django, `DEBUG = False` defaults it to 0.1, so
      9 in 10 fast requests are not stored. See [Sampling](how-it-works.md#sampling).
    - **Writer lag.** Traces are written by a background thread. Reload the page after
      a second.

## Find your first N+1

Both demo shops ship with deliberately bad endpoints. Clone the repository (see above)
and run one.

=== "Django"

    ```bash
    uv run python examples/django/manage.py migrate
    uv run python examples/django/manage.py seed       # ~400k reviews, a few seconds
    uv run python examples/django/manage.py runserver
    ```

    Visit `http://127.0.0.1:8000/orders/`, then open `http://127.0.0.1:8000/redline/`
    and sign in as `admin` / `admin` (a demo-only account). The request shows an N+1 on
    `shop_orderitem` with a `prefetch_related("items")` diff.

=== "FastAPI"

    ```bash
    cd examples/fastapi
    uv run --project ../.. python seed.py                      # creates shop.db
    uv run --project ../.. uvicorn shop.main:app --port 8000
    ```

    Visit `http://127.0.0.1:8000/orders`, then open `http://127.0.0.1:8000/_redline/`.
    The request shows an N+1 on order items with a `selectinload(Order.items)` diff.

=== "Flask"

    See `examples/flask/README.md` for the commands. The demo has deliberately bad views and
    shows the same N+1 with a `selectinload` diff.

Open the request, go to **Issues**, and copy the diff. The full tour of the demos' broken
endpoints is in each example's README (`examples/django/README.md`,
`examples/fastapi/README.md`, `examples/flask/README.md`).

## Your own spans

The only part of Redline your own code touches is `span`:

```python
import redline_core

@redline_core.span("pricing.compute")
def compute_price(order): ...
```

Outside a recorded request it does nothing. See [Custom spans](custom-spans.md).

## Configure it

Everything is optional. Settings are a dict:

=== "Django"

    ```python
    # settings.py
    REDLINE = {
        "SAMPLE_RATE": 1.0,
        "SLOW_QUERY_MS": 50,
        "IGNORE_PATHS": ["/static/", "/media/", "/favicon.ico", "^/health"],
        "PERMISSION": "myproject.redline.can_view",   # Django-only
    }
    ```

=== "FastAPI"

    ```python
    install(app, engine=engine, settings={
        "sample_rate": 1.0,
        "slow_query_ms": 50,
        "ignore_paths": ["/static/", "/favicon.ico", "^/health"],
    })
    ```

=== "Flask"

    ```python
    app.config["REDLINE"] = {
        "sample_rate": 1.0,
        "slow_query_ms": 50,
        "ignore_paths": ["/static/", "/favicon.ico", "^/health"],
    }
    Redline(app, engine=engine)
    ```

=== "Litestar"

    ```python
    RedlinePlugin(engine=engine, settings={
        "sample_rate": 1.0,
        "slow_query_ms": 50,
        "ignore_paths": ["/static/", "/favicon.ico", "^/health"],
    })
    ```

=== "redline.toml"

    The same settings, framework-independent, in a file at your project root:

    ```toml
    sample_rate = 1.0
    slow_query_ms = 50
    ignore_paths = ["/static/", "/favicon.ico", "^/health"]
    ```

Keys are case-insensitive (`SAMPLE_RATE` and `sample_rate` are the same) and an unknown
key is an error with a "did you mean" hint, so a typo never silently falls back to a
default. The complete table is the [Settings reference](settings.md). Presets,
`redline.toml` and `REDLINE_*` environment variables are covered in
[Configuration](configuration.md).

## Where things are stored

Traces go to `.redline/redline.db` (Django: under `BASE_DIR`; the others: relative to the
working directory), a SQLite file of Redline's own. Add `.redline/` to your
`.gitignore`. It holds redacted request data: see [Storage](storage.md) and
[Security](security.md).

## Next steps

- Understand what is recorded and what it costs: [How it works](how-it-works.md).
- Learn the UI: [The UI](the-ui.md).
- Fix something: [N+1 detection](n-plus-one.md), [EXPLAIN](explain.md).
- See what else it finds: [Detectors overview](detectors.md).
- Find out how much load you can take: [Load testing](load-testing.md).
- Before real traffic: [Production](production.md), [Access control](access-control.md)
  and [Security](security.md).
