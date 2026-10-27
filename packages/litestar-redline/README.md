<p align="center">
  <a href="https://pythondeveloper6.github.io/redline/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Pythondeveloper6/redline/main/docs/assets/logo-dark.svg">
      <img alt="Redline" src="https://raw.githubusercontent.com/Pythondeveloper6/redline/main/docs/assets/logo-light.svg" height="56">
    </picture>
  </a>
</p>

# litestar-redline

The Litestar + SQLAlchemy adapter for [Redline](https://github.com/Pythondeveloper6/redline#readme): a request profiler and
backend analyzer. Every request becomes a trace with a span timeline, the SQL it ran, N+1
and duplicate-query detection with **concrete fix diffs**, one-click `EXPLAIN`, optional
flamegraphs, and a fast web UI served from your own app.

```python
from litestar import Litestar
from sqlalchemy.ext.asyncio import create_async_engine
from litestar_redline import RedlinePlugin

engine = create_async_engine("sqlite+aiosqlite:///app.db")

app = Litestar(
    route_handlers=[...],
    # access="local": localhost only (unset, Redline answers 404 outside debug mode)
    plugins=[RedlinePlugin(engine=engine, settings={"access": "local"})],  # UI at http://localhost:8000/_redline/
)
```

Traces are stored in their own SQLite file (`.redline/redline.db`), never in your database.

## Install

```bash
pip install \
  "litestar-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/litestar-redline" \
  "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"
```

Not on PyPI yet: install from GitHub, naming `redline-core` in the same command (the adapter
pins it exactly). Docs: <https://pythondeveloper6.github.io/redline/>.

Add the extra to the core requirement for flamegraphs:
`"redline-core[profile] @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"`.

Python 3.10+, Litestar 2.12+ (< 3), SQLAlchemy 2.x.

## `RedlinePlugin`

```python
RedlinePlugin(
    settings=None,    # dict | redline_core.Settings
    engine=None,      # Engine | AsyncEngine | [engines] | {"alias": engine}
    path=None,        # default: the mount_path setting, else "/_redline"; never recorded
    guards=None,      # Litestar guards for the UI + API (see "Access control")
    store=None,       # custom Store (default: SQLite file at storage_path)
)
```

What it does when the app is built:

- adds a pure ASGI middleware (capture, route template, status, bodies, SQL spans);
- records exceptions through Litestar's `after_exception` hook (see below);
- mounts the UI and JSON API under `path` as a Litestar `Router`;
- instruments the engines, and **flushes and closes the store on shutdown**
  (`on_shutdown`; your own lifespan hooks keep working).

`settings` takes any [Redline setting](../redline-core/README.md) with case-insensitive keys:

```python
RedlinePlugin(engine=engine, settings={
    "sample_rate": 0.1,                 # keep 10% of requests ...
    "always_keep_slower_than_ms": 300,  # ... plus every slow one and every error
    "slow_query_ms": 50,
})
```

The plugin object is also the handle: `plugin.recorder`, `plugin.store`, `plugin.settings`,
`plugin.add_engine(engine)` for an engine created later, `plugin.close()`.

### Engines

With Litestar's own SQLAlchemy plugin (`advanced-alchemy`) and no `engine=`, the plugin's
engine(s) are found and instrumented automatically:

```python
from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig, SQLAlchemyPlugin

db = SQLAlchemyAsyncConfig(connection_string="sqlite+aiosqlite:///app.db")
app = Litestar([...], plugins=[SQLAlchemyPlugin(config=db), RedlinePlugin()])
```

Otherwise pass `engine=` yourself. The first engine of a list is the `"default"` db alias
(used by EXPLAIN); others are named after their database file, or pass a dict:

```python
RedlinePlugin(engine=[async_engine, sync_engine])
RedlinePlugin(engine={"main": main_engine, "analytics": analytics_engine})
```

## What is recorded

- **Request / response**: method, path, route template (`/orders/{order_id}`), handler
  dotted name (`shop.routes.get_order`, `shop.routes.ShopController.by_sku`), status,
  headers, query string and body. Secrets are redacted by the core.
- **Bodies are never consumed or buffered unboundedly.** The request body is copied as the
  app reads it; response bodies, including streams, are kept only up to `max_body_bytes`.
- **SQL** through `before/after_cursor_execute`: statement, bound params (redacted),
  duration, rowcount, vendor, executemany, `db_alias`, and the project line that issued the
  query, also through `AsyncSession`.
- **Exceptions.** Litestar turns an exception into a response *inside* the middleware, so
  the plugin registers an `after_exception` hook. A 5xx caused by an exception gets
  `error` (type, message, traceback, locals); a 4xx `HTTPException` (404, 401, validation)
  is a client error and is not an error trace.
- Sync handlers (`sync_to_thread=True`) are attributed to the right request.
- Your own spans: `with redline_core.span("pricing.compute"): ...`.
- `X-Redline-Run: <id>;step=<n>` becomes tags (used by the load tester).

Requests that match no route (404) and `405 Method Not Allowed` never reach a route-level
middleware, so Litestar does not run Redline for them.

### Profiling

Flamegraphs are opt-in per request: `?_redline=profile` or the header
`X-Redline-Profile: 1` (needs the `[profile]` extra).

### Outbound HTTP (httpx)

```python
from litestar_redline import instrument_httpx

instrument_httpx()   # every httpx.Client / AsyncClient call becomes an http_out span
```

## Fix suggestions

For an N+1, `SQLAlchemyFixSuggester` maps the repeated query back to your models and
produces a real unified diff (`selectinload(Parent.children)`, `joinedload(Child.parent)`),
exactly as in the FastAPI adapter.

## Access control

The UI exposes your SQL, request bodies and EXPLAIN, so treat it like an admin tool.

**Default.** With no `guards`, the `access` setting decides (default `local`, served only in debug mode until you set `access`): only loopback clients (127.0.0.0/8, `::1`; unix sockets only with `trust_unix_socket`) that
address the server by a loopback name (`Host: localhost`, `127.0.0.1`, `[::1]`, any port;
this blocks DNS rebinding) are served, and a warning is logged at startup. Refusals are
JSON `{"error": {"code": "forbidden" | "unauthorized" | ..., "message"}}`. Use
the `allowed_hosts` setting to allow another host name. Passing `guards=` implies
`access="framework"`. A loopback peer sending `X-Forwarded-For` without being in
`trusted_proxies` is refused; behind a reverse proxy use `access="token"` and
`trusted_proxies`. See the
[access control docs](https://pythondeveloper6.github.io/redline/access-control/).

**Your own guard** (any Litestar guard; it *replaces* the loopback default):

```python
from litestar.exceptions import NotAuthorizedException

def require_admin(connection, _handler):
    if not connection.user or not connection.user.is_admin:
        raise NotAuthorizedException()

RedlinePlugin(engine=engine, guards=[require_admin])
```

`guards=[]` is the same as passing none.

**CSRF.** The mutating endpoints (`DELETE` traces, `POST explain`) reject browser requests
whose `Origin` does not match the request host, `Origin: null`, and
`Sec-Fetch-Site: cross-site` or `same-site`. This is always on, whatever `guards` you pass.

Redline's own responses carry `X-Frame-Options: DENY`.

## EXPLAIN

`POST /_redline/api/explain` runs `EXPLAIN` for a recorded SELECT on the engine it came
from inside a transaction that is always rolled back (`READ ONLY` on PostgreSQL). `EXPLAIN
ANALYZE` executes the query, so it is off unless `explain_analyze=True`.

## Development

```bash
uv sync --all-packages
cd packages/litestar-redline && uv run pytest
```

## License

MIT
