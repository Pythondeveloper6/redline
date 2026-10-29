# FastAPI

[← Index](index.md)

`fastapi-redline` adds Redline to a FastAPI (or plain Starlette) application with
SQLAlchemy. One function, `install()`, does everything.

```python
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from fastapi_redline import install

app = FastAPI()
engine = create_async_engine("sqlite+aiosqlite:///app.db")

# access="local": localhost only (unset, Redline answers 404 outside debug mode)
install(app, engine=engine, settings={"access": "local"})  # UI at http://localhost:8000/_redline/
```

Requirements: Python 3.10+, FastAPI 0.110+ (below 1.0), SQLAlchemy 2.x, and
`redline-core` at the exact version the adapter pins. Install as shown in
[Getting started](getting-started.md#install).

Since 0.2 the SQLAlchemy and httpx hooks live in `redline_core.contrib` and are shared with
the Flask and Litestar adapters; `fastapi_redline` re-exports them (`instrument_engine`,
`instrument_httpx`), so existing imports keep working.

## `install()`

```python
install(
    app,
    engine=None,          # Engine | AsyncEngine | [engines] | {"alias": engine}
    settings=None,        # dict | redline_core.Settings
    prefix=None,          # default: the mount_path setting, else "/_redline"
    dependencies=None,    # auth dependencies for the UI and API
    models=None,          # Base / registry for fix suggestions (default: auto-discover)
    store=None,           # a custom Store (default: SQLite file at storage_path)
) -> Redline
```

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `app` | `FastAPI` | required | The application to instrument. Call after it is built, before it starts |
| `engine` | `Engine`, `AsyncEngine`, list, or `{alias: engine}` | `None` | Engines to record SQL from and run EXPLAIN on. See [Engines](#engines) |
| `settings` | `dict` or `redline_core.Settings` | `None` (defaults) | Any [setting](settings.md); keys are case-insensitive; unknown keys raise `SettingsError` |
| `prefix` | `str` or `None` | `None` | Where the UI and API are mounted. `None` means the `mount_path` setting, then `"/_redline"`. Must be a sub-path; `"/"` raises `ValueError` |
| `dependencies` | sequence of callables or `Depends(...)` | `None` | Protects the UI and API; a non-empty list implies `access = "framework"`. `None` or `[]` means the `access` policy decides (default `local`: **loopback only**, and only with `app.debug` until `access` is set). See [Access control](#access-control) |
| `models` | declarative `Base`, `registry`, mapped class, or an iterable of those | `None` | Where the fix suggester looks for relationships. Default: every registry SQLAlchemy knows |
| `store` | a `Store` | `None` | Replace the SQLite store. See [Extending](extending.md#the-store-protocol) |

It returns a `Redline` handle (also available as `app.state.redline`) and raises
`RuntimeError("Redline is already installed on this app.")` on a second call.

### What it does

1. Builds the settings and **appends a regex to `ignore_paths`** (`^/_redline(/|$)`) so
   Redline never records itself.
2. Creates the store (`SQLiteStore.from_settings(...)`) and a `SQLAlchemyFixSuggester`, and
   configures the process-wide recorder.
3. Instruments every engine you passed (registers them for EXPLAIN either way).
4. Mounts the UI and API router under `prefix` and **moves its routes to the front** of the
   app's route list, so a catch-all route you declared earlier cannot swallow it.
5. Adds the pure-ASGI `RedlineMiddleware`.

With `settings={"enabled": False}` steps 3 and 5 are skipped: no SQLAlchemy listeners and
no middleware, so a disabled install costs nothing per query (measured: under 1 µs, the
same as not installed). The UI still serves the traces already in the store.
6. **Wraps your lifespan**: on shutdown it flushes the pending traces and closes the store.
   An existing `lifespan=` keeps working and runs first.

### The `Redline` handle

| Member | Meaning |
|---|---|
| `redline.settings` | the effective `Settings` |
| `redline.store` | the store (`.count()`, `.flush()`, ...) |
| `redline.recorder` | the process recorder |
| `redline.api` | the `RedlineAPI` (call `redline.api.dispatch(...)` from a script or test) |
| `redline.engines` | the `{alias: engine}` mapping |
| `redline.add_engine(engine, alias=None)` | instrument another engine later (only registered for EXPLAIN when disabled); returns its alias |
| `redline.close()` | flush and close the store (called by the lifespan wrapper) |

## Engines

Redline sees SQL only for engines you give it. SQLAlchemy's cursor-level events fire for
both sync and async engines (the listeners go on `engine.sync_engine` for an
`AsyncEngine`), so `Session`, `AsyncSession`, `session.execute`, Core and ORM calls are
all recorded.

=== "Async engine"

    ```python
    engine = create_async_engine("postgresql+asyncpg://...")
    install(app, engine=engine)
    ```

=== "Sync engine"

    ```python
    engine = create_engine("postgresql+psycopg://...")
    install(app, engine=engine)
    ```

=== "Several engines"

    ```python
    install(app, engine=[async_engine, sync_engine])
    install(app, engine={"main": main_engine, "analytics": analytics_engine})
    ```

=== "Added later"

    ```python
    redline = install(app)
    ...
    redline.add_engine(late_engine)
    ```

### Aliases

Every query is stored with a `db_alias`, and EXPLAIN uses it to find the right engine.

| You pass | Aliases |
|---|---|
| one engine | `"default"` |
| a dict | its keys |
| a list | the first is `"default"`; the others are named after their database file (`.../analytics.db` becomes `analytics`), falling back to `db2`, `db3`, ... |

### Vendors

The dialect name maps to a vendor: `postgresql`, `mysql` (also MariaDB) and `sqlite` support
EXPLAIN; any other dialect records SQL (vendor `other`) but cannot be explained.

!!! note "PRAGMA statements are skipped"

    SQLite `PRAGMA` statements are not recorded. They are connection housekeeping, not
    your queries.

## Sync and async endpoints

Both work, and both attach queries to the right request.

- `async def` endpoints run on the event loop. Each request's trace lives in
  `contextvars`, so concurrent requests are isolated (there is a test for it).
- `def` endpoints run in Starlette's threadpool. Context is copied into the worker thread,
  so their queries attribute to the right request, also under concurrency.
- Work in a bare `threading.Thread` is **not** attributed to any request.

For `AsyncSession` the SQL actually runs inside a greenlet, below your `await`. Redline
bridges that boundary when it captures the callsite, so the reported line is still **your**
`await session.scalars(...)` and not SQLAlchemy's internals.

## What the middleware records

`RedlineMiddleware` is a **pure ASGI** middleware, not `BaseHTTPMiddleware`. The latter
wraps your app in extra tasks, breaks context propagation for sync endpoints, and
buffers streaming responses.

| Recorded | Notes |
|---|---|
| Method, path, **route template** (`/orders/{order_id}`) | Resolved from `scope["route"]` once routing ran. For mounted sub-applications the mount path is prefixed |
| Endpoint | Dotted name of the endpoint function (`shop.api.list_orders`) |
| Status, headers | The response status and headers |
| Request body | **Teed** as your app reads it, so the app sees the untouched `receive`. Kept up to `max_body_bytes` |
| Response body | A size-capped copy. A stream is never buffered beyond `max_body_bytes` |
| Unhandled exceptions | Recorded with the traceback, then **re-raised untouched** |
| Client cancellation | Recorded with status 499 when the client disconnects mid-request |
| `X-Redline-Run` | becomes the `run_id` and `step` tags (only for a run that is running in the store) |
| `?_redline=profile` / `X-Redline-Profile: 1` | runs the profiler, only with `app.debug` or valid Redline credentials (see [Forcing capture](security.md#forcing-capture)) |

What it does **not** do:

- **WebSockets and lifespan events** are passed straight through (only `http` scopes are
  recorded).
- **Compressed response bodies are not captured.** A response with a `Content-Encoding`
  header (for example from `GZipMiddleware` placed *outside* Redline) has no stored body.
  Order matters: add Redline so it is inside compression if you want bodies.
- It finishes the trace **right after the last response chunk is sent**, so background
  tasks do not inflate latency (and their queries are not in the trace).

!!! warning "Middleware order"

    Starlette runs the middleware added **last** outermost. `install()` calls
    `add_middleware` when it runs, so middleware you add *after* `install()` wraps
    Redline and its time is not measured; middleware added *before* is inside and is
    measured. Call `install()` last for the most complete picture.

## Outbound HTTP (httpx)

Opt in, once, at startup:

```python
from fastapi_redline import instrument_httpx

instrument_httpx()      # every httpx.Client / AsyncClient call becomes an http_out span
```

It wraps `httpx.Client.send` and `httpx.AsyncClient.send`, which every request method goes
through, so clients created by libraries are covered too. `install()` does not do it for
you, so the patch is always an explicit choice. It returns `False` if httpx is not
installed, is idempotent, and `uninstrument_httpx()` undoes it. Outside a traced request
the wrapper costs one `ContextVar.get()`.

Each span is named `GET host/path` and carries `method`, `url` and `status`. **URLs are
redacted**: `?api_key=...` is masked. `requests` is covered by [`instrument_requests()`](integrations.md#requests). `aiohttp` is not
instrumented.

## New in 0.2 for FastAPI

| Feature | Where to read |
|---|---|
| An event-loop lag monitor that names the blocking call in an `async def` endpoint | [Blocking in async](async-blocking.md) |
| `transaction` and `pool_wait` spans from SQLAlchemy | [Transactions and locks](transactions.md) |
| Response serialization (`serialize_response`, Pydantic) timed as a `serializer` span | [Slow serializers](serializers.md) |
| Row counts, large and unbounded results | [Large and unbounded results](result-size.md) |
| `requests`, Redis, asyncpg, psycopg, Marshmallow | [Integrations](integrations.md) |
| Strawberry GraphQL | [GraphQL](graphql.md) |
| `redline load init --from-openapi` against FastAPI's `/openapi.json` | [Load testing](load-testing.md#generating-scenarios) |
| `Server-Timing`, the badge, live mode, snapshots | [Server-Timing and the badge](server-timing-badge.md) |
| Presets, `redline.toml`, `REDLINE_*`, token and basic access, read-only | [Configuration](configuration.md), [Access control](access-control.md) |

## Access control

!!! danger "The UI exposes your SQL, request bodies and the ability to run EXPLAIN"

    Treat it as an admin tool. Decide who can reach it before you deploy.

### The default: loopback only, in debug mode

With no `dependencies`, the `access` setting decides. **While you have not set it**
(in code, `redline.toml` or `REDLINE_ACCESS`), Redline serves its `local` default only when
`app.debug` is on (`FastAPI(debug=True)`); otherwise every Redline URL answers `404` and an
error is logged at startup. Most apps run under plain `uvicorn` without `debug`, so set it:

```python
install(app, engine=engine, settings={"access": "local"})      # your laptop
install(app, engine=engine, settings={"access": "token", "token": "..."})  # a server
```

`local` allows a request only when **both** of these hold:

- the **client** is loopback: `127.0.0.0/8`, `::1`, IPv4-mapped IPv6 loopback, or
  Starlette's `TestClient` (a unix-socket peer only with `trust_unix_socket = true`);
- the **`Host` header** names loopback: `localhost`, any `*.localhost`, a `127.x.x.x`
  address or `[::1]`, with any port. This is what stops **DNS rebinding**: a malicious
  page that points its own domain at `127.0.0.1` reaches your server, but its requests
  say `Host: evil.example` and are refused.

Anyone else gets `403` with `{"error": {"code": "forbidden", "message": ...}}`, and a
startup warning is logged:

```text
Redline is mounted at /_redline with access='local': only loopback clients addressing
localhost are allowed. Set access='token' or 'basic', or pass install(app, dependencies=[...]),
before exposing it.
```

!!! warning "Behind a reverse proxy, 'loopback' may be everyone"

    If nginx or a load balancer forwards to your app over `127.0.0.1`, every request
    would look like localhost. Redline refuses a loopback peer that carries
    `X-Forwarded-For`, `X-Real-IP` or `Forwarded` unless it is listed in `trusted_proxies`,
    but the safe setup is `access = "token"` (or real dependencies) plus `trusted_proxies`.

To reach a local server through another name (a hosts-file alias, a tunnel), keep the
loopback rules and allow that name explicitly with the `allowed_hosts` setting:

```python
install(app, engine=engine, settings={"allowed_hosts": ["devbox"]})
```

(`loopback_guard(allowed_hosts=[...])` is still exported for use as an explicit
dependency.) The `local` policy treats Starlette's `TestClient` as loopback.

### Your own dependency

=== "Existing auth dependency"

    ```python
    from fastapi import Depends

    install(app, engine=engine, dependencies=[Depends(require_admin_user)])
    ```

=== "HTTP Basic"

    ```python
    import os, secrets
    from fastapi import Depends, HTTPException
    from fastapi.security import HTTPBasic, HTTPBasicCredentials

    basic = HTTPBasic()

    def redline_auth(creds: HTTPBasicCredentials = Depends(basic)) -> None:
        ok = secrets.compare_digest(creds.username, "admin") and secrets.compare_digest(
            creds.password, os.environ["REDLINE_PASSWORD"]
        )
        if not ok:
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    install(app, engine=engine, dependencies=[Depends(redline_auth)])
    ```

=== "A bare callable"

    ```python
    async def only_staff(request: Request) -> None:
        if not getattr(request.state, "is_staff", False):
            raise HTTPException(403)

    install(app, engine=engine, dependencies=[only_staff])   # wrapped in Depends for you
    ```

Refuse by **raising** `HTTPException` (the FastAPI way). A dependency that *returns*
`False` (`return user.is_admin`) is refused with `403` too, since FastAPI itself would
ignore the return value and let the request in; `None` and other values pass.

`dependencies=[]` is the same as not passing any (since 0.2: it used to leave the UI open).
To open it to everyone on a private network, use a dependency that returns nothing, or
`access = "open"` with `REDLINE_ALLOW_OPEN=1`. For tokens, basic auth,
`allowed_ips`, `read_only` and the rest, use the [`access` settings](access-control.md);
with an explicit policy other than `framework` your dependencies still run, after the gate.

The dependencies apply to **everything** under the prefix: the page, assets and the API.
An `HTTPException` they raise is answered in Redline's error shape,
`{"error": {"code": "unauthorized", "message": "..."}}` for a `401` (`forbidden` for
`403`), with its headers (`WWW-Authenticate`) kept, so the UI can show it. See
[HTTP API: errors](http-api.md#error-codes).

### CSRF and Origin

Every non-`GET` request (`DELETE api/traces...`, `POST api/explain`, login, logout) is
always protected by the gate, whatever your dependencies:

- a request with `Sec-Fetch-Site: cross-site` or `same-site` is rejected (`403 csrf_failed`);
- a request with an `Origin` header must match the request's `Host` (or
  `X-Forwarded-Host` from a trusted proxy); `Origin: null` is rejected;
- a request **without** `Origin` (curl, scripts) is allowed, because a web page cannot
  forge that, but your dependencies still apply.

Browsers always send `Origin` on those methods, so another website cannot drive your
Redline through a logged-in browser. This needs no change to the UI.

## Fix suggestions: SQLAlchemy

`SQLAlchemyFixSuggester` is registered for you. See
[N+1 detection](n-plus-one.md#how-the-fix-is-found) for what it produces. Pass `models=`
if your models live in a registry SQLAlchemy cannot discover.

SQLModel needs nothing: its tables live in a normal SQLAlchemy registry (or pass
`models=SQLModel`).

## EXPLAIN

`POST api/explain` runs on the engine the query came from, matched by `db_alias`, with the
captured SQL in the **driver's own paramstyle** through `exec_driver_sql`, in a
transaction that is always rolled back (and `SET TRANSACTION READ ONLY` on PostgreSQL).

- **Async engines** run on the application's event loop, where their connections belong:
  the loop is captured the first time the API is called.
- **Sync engines** run in a threadpool worker.
- If the engine behind an alias was never passed to `install()`, EXPLAIN answers
  `501 explain_unavailable`.

## Running with several workers

Each worker process has its own store connection and writer thread, and all write to the
**same** SQLite file. SQLite serialises writers (WAL mode, a 10-second busy timeout), so
this is intended to work. The store is **fork-safe**: with gunicorn `--preload` the store
built by `install()` in the master reopens its connection and starts its own writer in
each worker (see [Production: process models](production.md#process-models)). Busy
multi-worker deployments have not been load-tested, and each worker prunes independently. For anything beyond a development
`--workers 2`, prefer a single worker for the profiler, or per-worker `storage_path`s
(use the PID). A shared store for production is planned for [v1.0](roadmap.md).

## Testing your app with Redline installed

Starlette's `TestClient` reports its client address as `testclient`, which the loopback
check accepts, but it sends `Host: testserver`, which the DNS-rebinding check refuses.
Give the client a loopback base URL to open the UI in a test:
`TestClient(app, base_url="http://localhost")`. To keep test runs from writing a
file, set `storage_path` to `":memory:"`:

```python
install(app, engine=engine, settings={"storage_path": ":memory:"})
```

Or leave Redline out of the app under test, and install it only in your dev entry point.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `403 forbidden`: "only reachable from localhost" | you are not on loopback; pass `dependencies` |
| `403 forbidden`: "only answers requests addressed to localhost" | the `Host` header is not a loopback name (a hosts-file alias, a tunnel). Use `loopback_guard(allowed_hosts=[...])` |
| UI loads, but no requests appear | `install()` ran on a different app object than the one served; or the path is in `ignore_paths` |
| Requests appear with no SQL | the engine was not passed to `install()`/`add_engine()` |
| Requests have a `route` of `None` and group by raw path | the request matched no route (404), or a catch-all `Mount` swallowed routing |
| Callsites are empty | `project_root` is not an ancestor of your code. Set it |
| `RuntimeError: Redline is already installed` | `install()` was called twice (an auto-reloader importing twice?) |
| N+1 shown with a hint but no diff | the statement could not be located in source. See [confidence](n-plus-one.md#confidence) |


## Wiring an existing application

Pass the engine your application's session factory actually uses, after the routers and
models are imported and before serving requests. A different engine does not capture
those sessions. For an external launcher, set `project_root` to the application's root
so source attribution works. Keep the trace store outside source control.

```python
from app.main import app
from app.db.session import get_engine
from fastapi_redline import install, instrument_httpx

redline = install(app, engine=get_engine(), settings={
    "access": "local",
    "project_root": "/absolute/path/to/project",
})
instrument_httpx()  # optional: install() alone does not capture outbound HTTP
# If Redis is used, explicitly enable the redis-py integration too:
# from redline_core.contrib.redis import instrument_redis
# instrument_redis()
```

Use synthetic data and an isolated database for load tests. Missing external/cache spans
means the integration may be absent, not necessarily that the app made no calls. SDKs
without integrations and separate jobs require explicit instrumentation. Static source
analysis complements request capture; its candidates are not measured N+1 defects.
