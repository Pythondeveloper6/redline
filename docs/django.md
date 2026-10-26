# Django

[← Index](index.md)

`django-redline` adds Redline to a Django project: one app, one middleware and one URL
mount. It supports Django 5.2 (LTS), 6.0 and 6.1 on Python 3.12 and later, under both WSGI
and ASGI, with sync and async views.

```python
# settings.py
INSTALLED_APPS = [..., "django_redline"]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django_redline.middleware.RedlineMiddleware",     # as high as possible
    ...,
]

# urls.py
urlpatterns = [..., path("redline/", include("django_redline.urls"))]
```

Install it as described in [Getting started](getting-started.md#install). There are
**no migrations** and no models: Redline never touches your database.

## The pieces

| Piece | What it is |
|---|---|
| `"django_redline"` in `INSTALLED_APPS` | `RedlineConfig.ready()` installs the SQL, cache, template and view hooks, and registers the system checks |
| `django_redline.middleware.RedlineMiddleware` | starts and finishes a trace per request; sync and async capable |
| `include("django_redline.urls")` | serves the UI at the mount and the [JSON API](http-api.md) under `api/`. The URL namespace is `redline` (`redline:index`, `redline:asset`, `redline:api`) |
| `REDLINE = {...}` | optional settings; see the [reference](settings.md) |
| `django_redline.span` | re-export of [`redline_core.span`](custom-spans.md) |

## Middleware placement

Put `RedlineMiddleware` **near the top**, because the time of everything *below* it is part
of the trace and everything above it is not.

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # "django.middleware.gzip.GZipMiddleware",       # keep above Redline if you use it
    "django_redline.middleware.RedlineMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
```

| Rule | Why |
|---|---|
| **Below** `GZipMiddleware` | otherwise Redline sees compressed bodies (check `W002`) |
| **Above** session, common, CSRF, auth and messages middleware | so their time is in the trace (check `W003`) |
| After `SecurityMiddleware` | its redirects need no tracing |

The whole stack under Redline appears as a `middleware` span, with the `view` span inside
it.

## The URL mount

```python
from django.urls import include, path

urlpatterns = [
    path("redline/", include("django_redline.urls")),
]
```

Any prefix works: the UI uses hash routing and relative URLs. The **trailing slash
matters**: `/redline` redirects to `/redline/` through `APPEND_SLASH`. Requests under the
mount are **never recorded**; the middleware resolves the mount with
`reverse("redline:index")`.

If you mount it behind a `path("internal/", ...)` that your auth already protects, you get
that protection **in addition to** Redline's own permission check.

## Settings

```python
REDLINE = {
    "SAMPLE_RATE": 1.0,
    "SLOW_QUERY_MS": 50,
    "IGNORE_PATHS": ["/static/", "/media/", "/favicon.ico", "^/health"],
    "PERMISSION": "myproject.redline.can_view",
}
```

Every key is a [core setting](settings.md) (case-insensitive) except `PERMISSION` and
`STORE`. What Django adds:

| Behaviour | Detail |
|---|---|
| `SAMPLE_RATE` default | `1.0` with `DEBUG = True`, **`0.1` with `DEBUG = False`** |
| `STORAGE_PATH` default | `<BASE_DIR>/.redline/redline.db` when `BASE_DIR` is defined |
| `PROJECT_ROOT` default | `BASE_DIR`. It decides which stack frames count as your code |
| Errors | An unknown key or a bad value raises `ImproperlyConfigured` carrying the core message, and the system check `django_redline.E001` reports it |
| Paths | `STORAGE_PATH` and `PROJECT_ROOT` accept a string or a `pathlib.Path` (`BASE_DIR / ".redline" / "redline.db"`) |
| `STORE` | Where traces are kept, like FastAPI's `install(store=...)`. See [Custom store](#custom-store) |

The runtime (settings, store, recorder, API) is built **lazily on first use**, not in
`AppConfig.ready()`, so management commands like `migrate` never create `.redline/`. It is
rebuilt when `REDLINE`, `DEBUG` or `BASE_DIR` change through `setting_changed`, which is
what makes `override_settings(REDLINE=...)` work in tests.

## Custom store

`REDLINE["STORE"]` replaces the default `SQLiteStore.from_settings(settings)` with any
object implementing the [`Store` protocol](extending.md#the-store-protocol). It accepts:

| Value | Used as |
|---|---|
| a callable or a class, e.g. `MyStore` | called with the core `Settings`; the result is the store. Rebuilt (and the old one closed) whenever the runtime is rebuilt |
| a ready instance | used as-is. It outlives runtime rebuilds (`override_settings` only flushes it) and is closed at process exit |
| a dotted path to either, e.g. `"myproject.redline.make_store"` | imported, then as above |

```python
# myproject/redline.py
from redline_core.storage import SQLiteStore

class MirroringStore(SQLiteStore):
    def __init__(self, settings):
        super().__init__(
            settings.storage_path,
            queue_size=settings.writer_queue_size,
            max_rows=settings.max_rows,
            max_age_s=settings.max_age_s,
        )

    def save(self, trace):
        super().save(trace)
        send_somewhere_else(trace)                          # must not block

# settings.py
REDLINE = {"STORE": "myproject.redline.MirroringStore"}
```

An import error, or a value that does not produce a `Store`, raises
`ImproperlyConfigured` (and the system check `django_redline.E001`).

## Access control

Every view, page, assets and API, first checks `REDLINE["PERMISSION"]`. Before that, it
calls `request.get_host()`, so `ALLOWED_HOSTS` is enforced on Redline's views even without
`CommonMiddleware` (a defence against [DNS rebinding](security.md#dns-rebinding)).

| | Default | |
|---|---|---|
| Who | active staff: `user.is_active and user.is_staff` | |
| Anonymous, on the page | redirected to `admin:login` (or `LOGIN_URL` if the admin is not installed), with `?next=` | |
| Signed in without access, on the page | `403` plain text | |
| On the API | `403` JSON `{"error": {"code": "forbidden", ...}}` | |

Override it with a callable `(request) -> bool`, or its dotted path:

```python
# myproject/redline.py
def can_view(request):
    user = request.user
    return user.is_active and user.is_superuser
```

```python
REDLINE = {"PERMISSION": "myproject.redline.can_view"}
```

- A string that cannot be imported, or a value that is not callable, raises
  `ImproperlyConfigured` (and `E001`).
- An exception raised **inside** your callable **denies access** (fails closed).
- The check needs `request.user`, so keep `AuthenticationMiddleware` in `MIDDLEWARE`.
  Redline's own middleware does not need it.

See [Security](security.md) for the wider picture.

## CSRF

The page and `GET` calls need nothing. The mutating calls, `DELETE` (a trace, or all of
them) and `POST api/explain`, are protected by Django's own CSRF check, which Redline runs
on **every** API request **even if `CsrfViewMiddleware` is not in `MIDDLEWARE`**:

- the token must arrive in the CSRF header (`X-CSRFToken`, or whatever `CSRF_HEADER_NAME`
  says);
- on HTTPS Django's Origin and Referer checks also apply;
- a failure is a **JSON** `403` with code `csrf_failed`, not Django's HTML page.

The served page embeds a fresh token in `<meta name="redline-csrf" content="..."
data-header="...">`, and the SPA sends it back. Because the token is in the page and
not read from a cookie, this works with `CSRF_COOKIE_HTTPONLY = True` and with
`CSRF_USE_SESSIONS = True`.

Scripts calling the API must log in, keep the session cookie, and send the token.

## How it hooks in

Everything is installed once from `AppConfig.ready()` and is a single `ContextVar.get()` and
`None` check when nothing is being recorded.

| What | Hook | Span |
|---|---|---|
| **SQL** | a `connection.execute_wrapper` appended to **every** connection's `execute_wrappers`, through the `connection_created` signal and for connections already open | `sql` |
| **Cache** | the backends that `django.core.cache.caches` creates get their public methods wrapped | `cache` |
| **Templates** | `django.template.base.Template.render` (top-level templates and `{% include %}`) and the Jinja2 backend | `template` |
| **View** | `BaseHandler.make_view_atomic`, called by both the sync and the async handler right before the view | `view` |
| **Middleware** | the middleware itself wraps the rest of the stack | `middleware` |

Details worth knowing:

- Redline's own store is a separate `sqlite3` file, **not a Django connection**, so it can
  never record itself. Queries run by [EXPLAIN](explain.md) are explicitly suppressed.
- **Cache** spans carry `op` (`get`, `set`, `add`, `delete`, `touch`, `incr`, `decr`,
  `has_key`, `get_or_set`, `get_many`, `set_many`, `delete_many`, `clear`), the `key`
  (cut at 200 characters, or the first five keys plus a count for the `_many` calls),
  `hit` (for `get`, `get_many`, `has_key`) and `backend` (`default (LocMemCache)`). A cache
  backend object you instantiate **yourself**, not through `caches`, is not instrumented.
- **Template** spans are named after the template; queries run during rendering nest under
  them, which is how an N+1 in `{% for item in order.items.all %}` is attributed to the
  render.
- **View** spans are named by the dotted path (`shop.views.order_list`, or the class for
  class-based views) and carry `{"async": true|false}`.
- Queries issued from **library code** (a generic `ListView`, DRF, a lazily rendered
  `TemplateResponse`) have no project frame in their stack. They are attributed to the
  **project view** that handled the request, using the view's definition site. When the
  stack does not pass through the view (a serializer called by a DRF viewset), the view
  is added as the outermost frame so the [fix suggester](n-plus-one.md#how-the-fix-is-found)
  can find its queryset.

## What the middleware records

| Recorded | Notes |
|---|---|
| Method, path, **route template** (`/orders/<int:pk>/`), view | from `request.resolver_match` after the response |
| Request headers, query string | redacted before storage |
| Request body | read up front only if it is **not multipart**, has a `Content-Length` above 0 and at most `max_body_bytes`. Otherwise Redline does **not** read it (the view may stream it), and picks up whatever the view itself read |
| Response status, headers | `Set-Cookie` values from `response.cookies` are included, then redacted |
| Response body | only for non-streaming responses; **streaming and file responses are never consumed**. The size comes from `Content-Length` |
| Exceptions | `process_exception` remembers a view's exception for the trace and returns `None`: it **never handles or swallows** it. It becomes the trace's `error` only when the final response is a 5xx: `Http404`, `PermissionDenied`, `BadRequest` and friends turn into 4xx responses and are **not errors** (no `error` on the trace, not kept by `ALWAYS_KEEP_ERRORS`). An exception that escapes the stack is recorded as a 500 and re-raised |
| `X-Redline-Run` | becomes `run_id` and `step` tags |
| `?_redline=profile` (or `1`, `true`, `yes`, `on`) / `X-Redline-Profile: 1` | starts the [profiler](flamegraphs.md); the same values as FastAPI |

With `DEBUG = True`, a **kept** response gets an `X-Redline-Trace: <trace id>` header so you
can find it. It is never sent in production.

## Async views

`RedlineMiddleware` declares `sync_capable` and `async_capable`. Django adapts it to the
stack automatically: under ASGI with async middleware chain it runs its async path
(`__acall__`), under WSGI its sync path. In both, state is carried by `contextvars`.

```python
async def summary(request):
    n = await Order.objects.acount()          # recorded, attributed to this line
    return JsonResponse({"orders": n})
```

- Async ORM calls (`acount`, `aget`, `async for`) are recorded; they run through the same
  connection wrapper, via `sync_to_async`, which copies context.
- Concurrent async requests do not mix (there is a test for it).
- The view span of an async view has `async: true`.

## Django REST Framework

DRF views are ordinary Django views, so they are traced like any other. The extra value is
in the fix suggester: when a **serializer** reads a relation for every object it
serializes, the suggested diff targets the **view's** `queryset` or `get_queryset()`, and
the summary says which serializer does it:

```diff
 class OrderViewSet(viewsets.ModelViewSet):
-    queryset = Order.objects.all()
+    queryset = Order.objects.prefetch_related("items")
     serializer_class = OrderSerializer
```

(Shape only: illustrative, not output copied from a run.)

!!! note "How well tested is the DRF support?"

    The repository's tests exercise this with a small stand-in for `Serializer` and a
    viewset-shaped class, not with `rest_framework` itself, and the demo shop does not use
    DRF. The mechanism looks at class names ending in `Serializer` and at the view's
    `queryset`/`get_queryset`. It should hold for real DRF; it has not been run against it.

## Multiple databases

Every connection gets the wrapper, and each SQL span stores its `db_alias` (`default`,
`replica`, ...) and `vendor`. EXPLAIN runs on `connections[db_alias]`. If the span's alias
no longer exists, or the connection's vendor differs from the recorded one, EXPLAIN answers
`501 explain_unavailable`. Vendors other than PostgreSQL, MySQL and SQLite record SQL but
cannot be explained.

## System checks

`manage.py check` (and `runserver`) run these.

| ID | Level | Problem | Fix |
|---|---|---|---|
| `django_redline.E001` | Error | `REDLINE` is invalid: an unknown key (with a "did you mean"), a bad value, a bad `PERMISSION` | Fix the setting |
| `django_redline.E002` | Error | The storage location cannot be created or is not writable | Point `STORAGE_PATH` somewhere writable |
| `django_redline.W001` | Warning | `RedlineMiddleware` is not in `MIDDLEWARE`, so nothing is recorded | Add it near the top |
| `django_redline.W002` | Warning | `GZipMiddleware` is **after** Redline, so bodies are compressed | Move Redline below it |
| `django_redline.W003` | Warning | Redline is listed **after** session, common, CSRF, auth or messages middleware, so their time is not traced | Move Redline above them |
| `django_redline.W004` | Warning | `django_redline.urls` is not mounted, so the UI is unreachable | Add the `include()` |
| `django_redline.W005` | Warning | `DEBUG` is false and `SAMPLE_RATE` is `1.0` | Lower it, or leave it unset for `0.1` |

All are registered under the `compatibility` tag. `E002` and `W005` are only checked when
Redline is enabled; the others always run.

## Management commands

| Command | Does |
|---|---|
| `manage.py redline_clear [--noinput]` | Deletes every recorded trace. Asks for confirmation (`Delete all N Redline traces? [y/N]`) unless `--noinput` or `--no-input` |
| `manage.py redline_prune [--max-age-days N] [--max-rows N]` | Applies [retention](storage.md#retention) now. Defaults to the `MAX_AGE_DAYS` and `MAX_ROWS` settings. `0` disables that rule for this run (it never deletes everything: that is `redline_clear`). Negative values are an error |

Both build the runtime, flush pending writes, and print how many traces they removed. Run
`redline_prune` from cron if your app is idle for long stretches (the writer prunes as
it writes, so an idle app does not prune on its own).

```console
$ python manage.py redline_prune --max-age-days 1
Pruned 4120 trace(s); 880 left.
```

## Under test

`override_settings(REDLINE={...})` rebuilds the runtime. To keep test runs from writing a
file, use an in-memory store, and set `PROJECT_ROOT` to the code under test so test
functions themselves do not count as "your code":

```python
REDLINE = {
    "STORAGE_PATH": ":memory:",
    "SAMPLE_RATE": 1.0,
    "PROJECT_ROOT": str(BASE_DIR / "myapp"),
}
```

To assert on traces, read the store:

```python
from django_redline.runtime import get_runtime

rt = get_runtime()
rt.store.flush(5.0)
trace = rt.store.get(rt.store.list(None, limit=1).items[0]["id"])
assert not [i for i in trace.issues if i.kind == "n_plus_one"]
```

A pytest marker for this (`@pytest.mark.redline(no_n_plus_one=True)`) is planned for
[v0.3](roadmap.md).

## Deployment notes

- **WSGI (gunicorn, uWSGI)**: the runtime is built lazily in each worker, after the
  fork, and the store is fork-safe even if something built it before the fork
  (`--preload`). Each worker writes to the same file; see
  [Production: process models](production.md#process-models).
- **ASGI (uvicorn, daphne, hypercorn)**: nothing special.
- **Behind a proxy**: the Django permission uses your session, not the client address, so
  the FastAPI proxy caveat does not apply. `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`
  must include your host as usual.
- **Containers**: `.redline/` must be writable. With a read-only root filesystem point
  `STORAGE_PATH` at a mounted volume or `/tmp`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `manage.py check` warns `W001` | the middleware is missing |
| The UI says 404 at `/redline` | use `/redline/` (trailing slash), or `APPEND_SLASH = False` |
| 403 for a superuser | your `PERMISSION` callable returned false or raised (it fails closed). Test it in a shell |
| `403 csrf_failed` from a script | send the `X-CSRFToken` header and the session cookie |
| No requests appear | path in `IGNORE_PATHS`; `ENABLED` false; `SAMPLE_RATE` below 1 (default `0.1` with `DEBUG = False`) |
| Empty callsites | `PROJECT_ROOT` is not an ancestor of your code (set it as a string) |
| `ImproperlyConfigured: settings.REDLINE: ... must be a string` | a `Path` in `STORAGE_PATH` or `PROJECT_ROOT`: wrap it in `str()` |
| Queries from a thread missing | a bare `threading.Thread` does not carry the trace; use `contextvars.copy_context().run` |
