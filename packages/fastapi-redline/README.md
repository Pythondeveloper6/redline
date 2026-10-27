# fastapi-redline

The FastAPI + SQLAlchemy adapter for [Redline](../../README.md): a request profiler and
backend analyzer. Every request becomes a trace with a span timeline, the SQL it ran, N+1
and duplicate-query detection with **concrete fix diffs**, one-click `EXPLAIN`, optional
flamegraphs, and a fast web UI served from your own app.

```python
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from fastapi_redline import install

app = FastAPI()
engine = create_async_engine("sqlite+aiosqlite:///app.db")

install(app, engine=engine)      # UI at http://localhost:8000/_redline/
```

Traces are stored in their own SQLite file (`.redline/redline.db`), never in your database.

## Install

```bash
pip install \
  "fastapi-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/fastapi-redline" \
  "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"
```

Not on PyPI yet: install from GitHub, naming `redline-core` in the same command (the adapter
pins it exactly). Docs: <https://pythondeveloper6.github.io/redline/>.

Add the extra to the core requirement for flamegraphs:
`"redline-core[profile] @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"`.

Python 3.10+, FastAPI 0.110+, SQLAlchemy 2.x.

## `install()`

```python
install(
    app,
    engine=None,          # Engine | AsyncEngine | [engines] | {"alias": engine}
    settings=None,        # dict | redline_core.Settings
    prefix="/_redline",
    dependencies=None,    # auth dependencies for the UI + API (see below)
    models=None,          # Base / registry for fix suggestions (default: auto-discover)
    store=None,           # custom Store (default: SQLite file at storage_path)
) -> Redline
```

What it does:

- configures the recorder and the store, and **flushes and closes the store on shutdown**
  (it wraps your lifespan; an existing `lifespan=` keeps working);
- adds a pure ASGI middleware (not `BaseHTTPMiddleware`);
- instruments the engines;
- mounts the UI and JSON API under `prefix` (ahead of any catch-all route you declared).

Call it after your app is built, before it starts. `settings` takes any
[Redline setting](../redline-core/README.md) with case-insensitive keys:

```python
install(app, engine=engine, settings={
    "sample_rate": 0.1,                 # keep 10% of requests ...
    "always_keep_slower_than_ms": 300,  # ... plus every slow one and every error
    "slow_query_ms": 50,
})
```

### Multiple engines

The first engine of a list is the `"default"` db alias; others are named after their
database file, or pass a dict. The alias is stored on each query so EXPLAIN runs on the right
engine.

```python
install(app, engine=[async_engine, sync_engine])
install(app, engine={"main": main_engine, "analytics": analytics_engine})
redline = install(app)
redline.add_engine(late_engine)            # instrument an engine created later
```

### SQLModel

Nothing to do: SQLModel tables live in a regular SQLAlchemy registry, which the fix
suggester discovers (or pass `models=SQLModel`).

## What is recorded

- **Request / response**: method, path, route template (`/orders/{order_id}`), endpoint
  dotted name, status, headers, query string and body. Secrets are redacted by the core
  (`Authorization`, cookies, `password`/`token`/`card` keys...).
- **Bodies are never consumed or buffered unboundedly.** The request body is copied as the
  app reads it; response bodies, including streams, are kept only up to `max_body_bytes`.
- **SQL** through `before/after_cursor_execute`: statement, bound params (redacted),
  duration, rowcount, vendor, executemany, `db_alias`, and the project line that issued the
  query, also through `AsyncSession` (the greenlet boundary is bridged).
- **Unhandled exceptions** are recorded and re-raised untouched.
- Spans from `def` endpoints running in Starlette's threadpool attach to the right request
  (context is copied to the worker thread; tested under concurrency).
- The trace is finished when the last response chunk is sent, so background tasks do not
  inflate latency. Work that starts a bare `threading.Thread` is not attributed to any
  request.
- Your own spans: `with redline_core.span("pricing.compute"): ...` (also `async with` and
  as a decorator).
- `X-Redline-Run: <id>;step=<n>` becomes tags (used by the load tester).

### Outbound HTTP (httpx)

Opt-in, once at startup:

```python
from fastapi_redline import instrument_httpx

instrument_httpx()   # every httpx.Client / AsyncClient call becomes an http_out span
```

URLs are redacted (`?api_key=...` is masked). Use `uninstrument_httpx()` to undo it.

### Profiling

Flamegraphs are opt-in per request: add `?_redline=profile` or the header
`X-Redline-Profile: 1` (needs the `[profile]` extra). They are recorded with pyinstrument in
async mode, so concurrent requests do not pollute each other.

## Fix suggestions

For an N+1, `SQLAlchemyFixSuggester` maps the repeated query back to your models and tells
you how to eager-load it:

| Repeated query | Suggestion |
|---|---|
| child table filtered by its foreign key | `selectinload(Parent.children)` |
| parent looked up by primary key, once per row | `joinedload(Child.parent)` |
| association (secondary) table filtered by the parent | `selectinload(Parent.tags)` |

It then walks the call stack to find the `select(Parent)` or `session.query(Parent)` in
your source and produces a real unified diff:

```diff
-        orders = (await session.scalars(select(Order).order_by(Order.id).limit(30))).all()
+        orders = (await session.scalars(select(Order).order_by(Order.id).limit(30).options(selectinload(Order.items)))).all()
```

plus the `from sqlalchemy.orm import selectinload` line when it is missing. When the
statement cannot be located (it lives in another module, or is built dynamically) you get
the one-line code hint with lower confidence instead. Both the lazy-load form
(`order.items`) and explicit per-row queries are recognised.

## Access control

The UI exposes your SQL, request bodies and EXPLAIN, so treat it like an admin tool.

**Default.** With no `dependencies`, only loopback clients (127.0.0.0/8, `::1`, unix sockets)
that address the server by a loopback name (`Host: localhost`, `127.0.0.1`, `[::1]`, any port;
this blocks DNS rebinding)
are served and a warning is logged at startup. Refusals, and `HTTPException`s from your own
dependencies, are JSON `{"error": {"code": "forbidden" | "unauthorized" | ..., "message"}}`.
Use `Depends(loopback_guard(allowed_hosts=["devbox"]))` to allow another host name. Behind a
reverse proxy every client may look like localhost: pass real dependencies instead, or run
uvicorn with `--proxy-headers` and `--forwarded-allow-ips` so the client address is the real one.

**Your existing auth dependency:**

```python
from fastapi import Depends

install(app, engine=engine, dependencies=[Depends(require_admin_user)])
```

**HTTP Basic:**

```python
import secrets
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

(`dependencies=[]` leaves it open on purpose. Don't, outside a private network.)

**CSRF.** The mutating endpoints (`DELETE` traces, `POST explain`) reject browser requests
whose `Origin` does not match the request host, `Origin: null`, and
`Sec-Fetch-Site: cross-site`. Browsers always send `Origin` on those methods, so another
website cannot drive your Redline through a logged-in browser. Non-browser clients (curl,
scripts) send no `Origin` and are only subject to your auth dependencies.

## EXPLAIN

`POST /_redline/api/explain` runs `EXPLAIN` for a recorded SELECT on the engine it came
from (matched by `db_alias`) inside a transaction that is always rolled back (and
`READ ONLY` on PostgreSQL). The captured SQL is in the driver's paramstyle and goes
through `exec_driver_sql`. Async engines run on the application's event loop. `EXPLAIN
ANALYZE` executes the query, so it is off unless `explain_analyze=True`.

## Development

```bash
uv sync --all-packages
cd packages/fastapi-redline && uv run pytest
```

A runnable demo with deliberately bad endpoints lives in [`examples/fastapi`](../../examples/fastapi).

## License

MIT
