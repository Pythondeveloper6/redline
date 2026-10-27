# Litestar

[← Index](index.md)

`litestar-redline` is the Litestar and SQLAlchemy adapter. It is a Litestar plugin: it adds
a pure ASGI middleware, hooks Litestar's exception handling, mounts the UI as a `Router`
and instruments your SQLAlchemy engines, sync or async.

## Install

```bash
pip install \
  "litestar-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/litestar-redline" \
  "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"
```

Python 3.10 or newer, Litestar 2.12 or newer (below 3), SQLAlchemy 2.x. Add the `profile`
extra to the core requirement for [flamegraphs](flamegraphs.md).

## Mount it

```python
from litestar import Litestar
from sqlalchemy.ext.asyncio import create_async_engine
from litestar_redline import RedlinePlugin

engine = create_async_engine("sqlite+aiosqlite:///app.db")

app = Litestar(
    route_handlers=[...],
    plugins=[RedlinePlugin(engine=engine)],   # UI at http://localhost:8000/_redline/
)
```

Traces go to their own SQLite file (`.redline/redline.db`), never to your database.

## `RedlinePlugin`

```python
RedlinePlugin(
    settings=None,      # dict | redline_core.Settings
    engine=None,        # Engine | AsyncEngine | [engines] | {"alias": engine}
    path="/_redline",   # where the UI and API live; never recorded
    guards=None,        # Litestar guards for the UI and API
    store=None,         # a custom Store; default is the SQLite file
)
```

When the app is built the plugin:

- adds a pure ASGI middleware (capture, route template, status, bodies, SQL spans);
- records exceptions through Litestar's `after_exception` hook;
- mounts the UI and JSON API under `path` as a `Router`;
- instruments the engines, and **flushes and closes the store on shutdown**
  (`on_shutdown`; your own lifespan hooks keep working).

The plugin object is also the handle: `plugin.recorder`, `plugin.store`,
`plugin.settings`, `plugin.add_engine(engine)` for an engine created later and
`plugin.close()`.

```python
RedlinePlugin(engine=engine, settings={
    "sample_rate": 0.1,                 # keep 10% of requests ...
    "always_keep_slower_than_ms": 300,  # ... plus every slow one and every error
    "slow_query_ms": 50,
})
```

<!-- verify: configuration -->
Presets, `redline.toml` and `REDLINE_*` environment variables apply too. See
[Configuration](configuration.md).

### Engines

With Litestar's own SQLAlchemy plugin (`advanced-alchemy`) and no `engine=`, that plugin's
engines are found and instrumented automatically:

```python
from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig, SQLAlchemyPlugin

db = SQLAlchemyAsyncConfig(connection_string="sqlite+aiosqlite:///app.db")
app = Litestar([...], plugins=[SQLAlchemyPlugin(config=db), RedlinePlugin()])
```

Otherwise pass `engine=` yourself. The first engine of a list is the `"default"` alias used
by EXPLAIN; others are named after their database file, or pass a dict:

```python
RedlinePlugin(engine=[async_engine, sync_engine])
RedlinePlugin(engine={"main": main_engine, "analytics": analytics_engine})
```

## What is recorded

- **Request and response**: method, path, route template (`/orders/{order_id}`), handler
  dotted name (`shop.routes.get_order`, `shop.routes.ShopController.by_sku`), status,
  headers, query string and body. Secrets are [redacted](security.md#redaction).
- **Bodies are never consumed or buffered without bound.** The request body is copied as
  the app reads it; response bodies, including streams, are kept only up to
  `max_body_bytes`.
- **SQL** through `before/after_cursor_execute`, including through `AsyncSession`:
  statement, bound parameters (redacted), duration, rows, vendor, `db_alias` and the project
  line that issued it.
- **Transactions and pool waits** as `transaction` and `pool_wait` spans
  ([details](transactions.md)).
- **Event-loop blocking.** A synchronous call inside an `async def` handler stalls the loop
  for every request. Redline notices and names the call. See
  [Blocking in async](async-blocking.md).
- **Exceptions.** Litestar turns an exception into a response *inside* the middleware, so
  the plugin registers an `after_exception` hook. A 5xx caused by an exception gets `error`
  (type, message, traceback and [locals](logs-and-exceptions.md#exceptions-with-locals)); a 4xx
  `HTTPException` (404, 401, validation) is a client error and is not an error trace.
- **Marshmallow `dump`** as a `serializer` span, if you opt in
  ([Integrations](integrations.md#marshmallow)). Litestar's own DTO and msgspec encoding
  is not timed separately.
- Sync handlers (`sync_to_thread=True`) are attributed to the right request.
- Your own spans: `with redline_core.span("pricing.compute"): ...`.
- `X-Redline-Run: <id>;step=<n>` becomes tags (used by the [load tester](load-testing.md)).

Requests that match no route (404) and `405 Method Not Allowed` never reach a route-level
middleware, so Litestar does not run Redline for them.

## Outbound HTTP

```python
from litestar_redline import instrument_httpx

instrument_httpx()   # every httpx.Client / AsyncClient call becomes an http_out span
```

Other clients (`requests`), Redis and psycopg are covered in [Integrations](integrations.md).

## Profiling

Flamegraphs are opt-in per request: `?_redline=profile` or the header
`X-Redline-Profile: 1`. See [Flamegraphs](flamegraphs.md).

## Fix suggestions

For an N+1, `SQLAlchemyFixSuggester` maps the repeated query to your models and produces a
real unified diff (`selectinload(Parent.children)`, `joinedload(Child.parent)`), exactly as
in the [FastAPI adapter](fastapi.md#fix-suggestions-sqlalchemy).

## OpenAPI and load testing

Litestar serves an OpenAPI schema, so you can generate a load-test scenario from it:

```bash
redline load init --from-openapi http://localhost:8000/schema/openapi.json
```

See [Load testing](load-testing.md#generating-scenarios).

## Server-Timing, the badge and live mode

<!-- verify: adapter wiring (Server-Timing, badge, SSE, snapshot) -->
Every recorded response carries a `Server-Timing` header, HTML pages get the floating badge
in debug mode, `api/stream` feeds [Live mode](the-ui.md#live-mode) and the snapshot
download is served as-is. See [Server-Timing and the badge](server-timing-badge.md).

## Access control

The UI exposes your SQL, request bodies and EXPLAIN, so treat it like an admin tool.

**Default.** With no `guards`, only loopback clients (`127.0.0.0/8`, `::1`, unix sockets)
that address the server by a loopback name (`Host: localhost`, `127.0.0.1`, `[::1]`, any
port; this blocks DNS rebinding) are served, and a warning is logged at startup. Refusals
are JSON: `{"error": {"code": "forbidden" | "unauthorized" | ..., "message": "..."}}`.
Use `guards=[loopback_guard(allowed_hosts=["devbox"])]` to accept another host name.

**Your own guard.** Any Litestar guard. It **replaces** the loopback default:

```python
from litestar.exceptions import NotAuthorizedException

def require_admin(connection, _handler):
    if not connection.user or not connection.user.is_admin:
        raise NotAuthorizedException()

RedlinePlugin(engine=engine, guards=[require_admin])
```

`guards=[]` leaves the UI open on purpose. Do not do that outside a private network.

<!-- verify: access control -->
For tokens, basic auth, IP allow-lists, trusted proxies and read-only mode, use the
`access` family of settings. They behave the same on every framework: see
[Access control](access-control.md).

**CSRF.** The mutating endpoints (`DELETE`, `POST`) reject browser requests whose `Origin`
does not match the request host, `Origin: null`, and `Sec-Fetch-Site: cross-site`. This is
always on, whatever `guards` you pass. Redline's own responses carry
`X-Frame-Options: DENY`.

!!! tip "Behind a reverse proxy"

    Every client may look like localhost. Pass real guards, or run uvicorn with
    `--proxy-headers` and `--forwarded-allow-ips`. See [Access control](access-control.md#behind-a-reverse-proxy).

## EXPLAIN

`POST /_redline/api/explain` runs `EXPLAIN` for a recorded SELECT on the engine it came
from, inside a transaction that is always rolled back (`READ ONLY` on PostgreSQL).
`EXPLAIN ANALYZE` executes the query, so it is off unless `explain_analyze=True`. See
[EXPLAIN](explain.md).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| No requests appear | The path starts with `path` (never recorded), matches `ignore_paths`, was sampled out, or is a 404 that matched no route |
| No SQL appears | Pass `engine=`, or use `advanced-alchemy`'s plugin so engines are discovered |
| The UI answers 403 | Not a loopback client or `Host`. Pass `guards=` or configure [access](access-control.md) |
| Handler shows the wrong name | Controllers are named `module.Controller.method`; check `GET api/traces/<id>` for `view` |
