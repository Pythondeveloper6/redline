<p align="center">
  <a href="https://pythondeveloper6.github.io/redline/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Pythondeveloper6/redline/main/docs/assets/logo-dark.svg">
      <img alt="Redline" src="https://raw.githubusercontent.com/Pythondeveloper6/redline/main/docs/assets/logo-light.svg" height="56">
    </picture>
  </a>
</p>

# flask-redline

The Flask + SQLAlchemy adapter for [Redline](https://github.com/Pythondeveloper6/redline#readme): a request profiler and
backend analyzer. Every request becomes a trace with a span timeline, the SQL it ran, N+1
and duplicate-query detection with **concrete fix diffs**, one-click `EXPLAIN`, optional
flamegraphs, and a fast web UI served from your own app.

```python
from flask import Flask
from sqlalchemy import create_engine
from flask_redline import Redline

app = Flask(__name__)
engine = create_engine("sqlite:///app.db")

# access="local": localhost only (unset, Redline answers 404 outside debug mode)
Redline(app, engine=engine, settings={"access": "local"})  # UI at http://localhost:5000/_redline/
```

With **Flask-SQLAlchemy** you do not even pass the engine: its engines (including
`SQLALCHEMY_BINDS`) are found and instrumented automatically.

```python
db = SQLAlchemy(app)
Redline(app)
```

Traces are stored in their own SQLite file (`.redline/redline.db`), never in your database.

## Install

```bash
pip install \
  "flask-redline @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/flask-redline" \
  "redline-core @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"
pip install "flask[async]"      # only if you use `async def` views
```

Not on PyPI yet: install from GitHub, naming `redline-core` in the same command (the adapter
pins it exactly). Docs: <https://pythondeveloper6.github.io/redline/>.

Add the extra to the core requirement for flamegraphs:
`"redline-core[profile] @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"`.

Python 3.10+, Flask 3.0+, SQLAlchemy 2.x.

## `Redline`

```python
Redline(
    app=None,
    *,
    engine=None,          # Engine | [engines] | {"alias": engine} (default: Flask-SQLAlchemy's)
    settings=None,        # dict | redline_core.Settings
    url_prefix=None,      # default: the mount_path setting, else "/_redline"
    permission=None,      # callable(request) -> bool (see "Access control")
    store=None,           # custom Store (default: SQLite file at storage_path)
)
redline.init_app(app)     # application factories
```

What it does:

- wraps `app.wsgi_app` in a WSGI middleware that times the request and records status,
  headers and (size-capped) bodies;
- connects Flask hooks for the route template (`/orders/<int:order_id>`), the view
  function, templates and unhandled errors;
- instruments the engines;
- registers a blueprint with the UI and JSON API under `url_prefix`, and the
  `flask redline` command group.

`settings` takes any [Redline setting](../redline-core/README.md) with case-insensitive
keys. They can also live in `app.config["REDLINE"]` (a dict); keys passed to `Redline`
win. A `Settings` instance passed to `Redline` is used as-is.

```python
Redline(app, engine=engine, settings={
    "sample_rate": 0.1,                 # keep 10% of requests ...
    "always_keep_slower_than_ms": 300,  # ... plus every slow one and every error
    "slow_query_ms": 50,
})
```

### Application factory

```python
redline = Redline(engine=engine)

def create_app():
    app = Flask(__name__)
    redline.init_app(app)
    return app
```

`redline.store`, `.settings`, `.engines`, `.add_engine(engine, alias=None)` and `.close()`
act on the current app (inside an app context) or on the last app initialised. The
store is flushed and closed at interpreter exit.

### Engines

The first engine of a list is the `"default"` db alias; others are named after their
database file, or pass a dict. The alias is stored on each query so EXPLAIN runs on the
right engine.

```python
Redline(app, engine=[main_engine, analytics_engine])
Redline(app, engine={"main": main_engine, "analytics": analytics_engine})
redline.add_engine(late_engine)            # an engine created later
```

When `engine` is omitted and Flask-SQLAlchemy 3.x is set up on the app, its engines are
discovered on the first request (they may not exist yet when `Redline(app)` runs), named
`default` and after each bind key.

## What is recorded

- **Request / response**: method, path, route template, view dotted name, status, headers,
  query string and body. Secrets are redacted by the core (`Authorization`, cookies,
  `password`/`token`/`card` keys...).
- **Bodies are never consumed or buffered unboundedly.** The request body is copied as the
  view reads it; response bodies, including streams, are kept only up to `max_body_bytes`.
  A `stream_with_context` generator is not read ahead, and the trace ends when the last
  chunk is sent, so its work counts towards the latency. `send_file` and anything the
  server serves through `wsgi.file_wrapper` (`direct_passthrough`) is handed back
  untouched: only its size is recorded.
- **SQL** through `before/after_cursor_execute`: statement, bound params (redacted),
  duration, rowcount, vendor, executemany, `db_alias`, and the project line that issued the
  query.
- **Spans**: a `view` span around the view function (not the `before_request` hooks), and a
  `template` span per `render_template` / `render_template_string`; queries a template
  triggers (lazy loading) nest under it.
- **Unhandled exceptions** are recorded (Flask's `got_request_exception`) and re-raised or
  turned into a 500 exactly as before. `abort(404)` and other 4xx `HTTPException`s are
  client errors, not failures: the trace has no `error`, and `always_keep_errors` does not
  keep it. A 5xx status is an error.
- **`async def` views** (Flask's asgiref bridge) keep the trace: queries and spans created
  inside the coroutine land in the request's trace. Work that starts a bare
  `threading.Thread` is not attributed to any request.
- Your own spans: `with redline_core.span("pricing.compute"): ...` (also as a decorator).
- `X-Redline-Run: <id>;step=<n>` becomes tags (used by the load tester).
- `X-Redline-Check: <id>;req=<i>;n=<k>` becomes tags (used by `redline check`, only for a check run that exists in the store).
- Requests under `url_prefix` are never recorded.

If you put `werkzeug.middleware.proxy_fix.ProxyFix` (or any other WSGI middleware) around
the app, wrap *after* creating `Redline` so the recorded client address is the real one.
Apps mounted under a `SCRIPT_NAME` (`DispatcherMiddleware`) record the full path and route.

### Outbound HTTP (opt-in)

Nothing is patched unless you ask:

```python
from flask_redline import instrument_httpx, instrument_requests

instrument_httpx()      # every httpx.Client / AsyncClient call becomes an http_out span
instrument_requests()   # same for requests.Session.send (needs `requests` installed)
```

URLs are redacted (`?api_key=...` is masked). `uninstrument_httpx()` and
`uninstrument_requests()` undo it.

### Profiling

Flamegraphs are opt-in per request: add `?_redline=profile` or the header
`X-Redline-Profile: 1` (needs the `[profile]` extra).

## Fix suggestions

For an N+1, `SQLAlchemyFixSuggester` maps the repeated query back to your models and tells
you how to eager-load it:

| Repeated query | Suggestion |
|---|---|
| child table filtered by its foreign key | `selectinload(Parent.children)` |
| parent looked up by primary key, once per row | `joinedload(Child.parent)` |
| association (secondary) table filtered by the parent | `selectinload(Parent.tags)` |

It then walks the call stack to find the `select(Parent)` or `session.query(Parent)` in
your source and produces a real unified diff. This works the same with Flask-SQLAlchemy's
`db.session` and `db.Model`. When the statement cannot be located you get the one-line
code hint with lower confidence instead.

## Access control

The UI exposes your SQL, request bodies and EXPLAIN, so treat it like an admin tool.

**Default.** With no `permission`, the `access` setting decides (default `local`, served only in debug mode until you set `access`): only loopback clients (127.0.0.0/8, `::1`; unix sockets only with `trust_unix_socket`)
that address the server by a loopback name (`Host: localhost`, `127.0.0.1`, `[::1]`, any
port; this blocks DNS rebinding) are served, and a warning is logged at startup. Refusals
are JSON `{"error": {"code": "forbidden", "message"}}`. Add other host names with the
`allowed_hosts` setting. A loopback peer sending `X-Forwarded-For` without being in
`trusted_proxies` is refused; behind a reverse proxy use `access="token"` and
`trusted_proxies`. Tokens, basic auth, `allowed_ips` and `read_only` are described in the
[access control docs](https://pythondeveloper6.github.io/redline/access-control/).

**Your own check.** `permission` is any `callable(request) -> bool`, called with Flask's
`request`; passing it implies `access="framework"` and replaces the loopback default, so the
`Host` check becomes yours (the core gate still applies `allowed_ips`, `read_only`, features
and CSRF).

```python
from flask_login import current_user

Redline(app, engine=engine, permission=lambda request: current_user.is_admin)
```

Raise `werkzeug.exceptions.abort(401)` (or any `HTTPException`) inside it to answer with
that status; it is rendered as `{"error": {"code": "unauthorized", ...}}`, keeping a
`WWW-Authenticate` header.

**CSRF.** Every non-GET request is refused with `403 csrf_failed` when it carries
`Sec-Fetch-Site: cross-site` or `same-site`, or an `Origin` (including `null`) that does not
match the request host. Browsers always send `Origin` on those methods, so another website
cannot drive your Redline through a logged-in browser. Non-browser clients (curl, scripts)
send no `Origin` and are only subject to the access policy.

Redline's own responses carry `X-Frame-Options: DENY`.

## EXPLAIN

`POST /_redline/api/explain` runs `EXPLAIN` for a recorded SELECT on the engine it came
from (matched by `db_alias`) inside a transaction that is always rolled back (and
`READ ONLY` on PostgreSQL). `EXPLAIN ANALYZE` executes the query, so it is off unless
`explain_analyze=True`.

## Command line

```bash
flask redline clear [--yes]                          # delete every recorded trace
flask redline prune [--max-age-days N] [--max-rows N]   # apply retention now (0 disables a rule)
```

## Development

```bash
uv sync --all-packages
cd packages/flask-redline && uv run pytest
```

A runnable demo with deliberately bad views lives in [`examples/flask`](https://github.com/Pythondeveloper6/redline/blob/main/examples/flask).

## License

MIT
