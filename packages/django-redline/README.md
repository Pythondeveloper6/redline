# django-redline

A modern request profiler for Django: SQL timelines, **N+1 detection with ready-to-apply
`select_related`/`prefetch_related` diffs**, one-click EXPLAIN, flamegraphs and a fast web UI.
It is async-correct (WSGI and ASGI, sync and async views). It does not touch your database:
traces live in their own SQLite file.

It replaces django-silk. Redline stores redacted data rather than plaintext secrets, never writes to
your app DB or records itself, supports async, groups N+1s, and adds EXPLAIN and flamegraphs.

> Status: `0.1.0a1`, alpha. Built on [`redline-core`](../redline-core).

## Install

```bash
pip install django-redline            # or: pip install "django-redline[profile]" for flamegraphs
```

```python
# settings.py
INSTALLED_APPS = [..., "django_redline"]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # "django.middleware.gzip.GZipMiddleware",        # if you use it, keep it above Redline
    "django_redline.middleware.RedlineMiddleware",     # as high as possible
    "django.contrib.sessions.middleware.SessionMiddleware",
    ...
]

# urls.py
from django.urls import include, path

urlpatterns = [
    ...,
    path("redline/", include("django_redline.urls")),
]
```

Run your app, make some requests and open **`/redline/`** as a staff user.

## What you get

- **Requests**: every request with method, route template (`/orders/<int:pk>/`), view, status,
  duration, query count and issue badges. Filter, search and live polling.
- **Request detail**: a waterfall of the span tree (middleware, then view, then SQL / cache / template
  spans, plus your own `span()`s), SQL grouped by fingerprint with the project file:line that ran it,
  redacted headers and bodies, and the traceback of unhandled exceptions.
- **Issues**: N+1s, duplicate queries, slow queries and query budgets, deduplicated across requests.
- **The Django fix suggester** maps the repeated SQL back to your models and relations, then finds
  the queryset in your source and writes the patch:

  ```diff
  --- a/shop/views.py
  +++ b/shop/views.py
  @@ -32,5 +32,5 @@
   def order_list(request):
  -    orders = Order.objects.select_related("customer").order_by("-id")[:30]
  +    orders = Order.objects.select_related("customer").prefetch_related("items").order_by("-id")[:30]
  ```

  | Situation | Suggestion |
  |---|---|
  | reverse FK (`order.items.all()`) | `prefetch_related("items")` |
  | forward FK / one-to-one (`item.product`) | `select_related("product")` |
  | reverse one-to-one (`customer.profile`) | `select_related("profile")` |
  | many-to-many, either side | `prefetch_related("tags")` / `prefetch_related("products")` |
  | nested loops (`for item in order.items.all(): item.product`) | `prefetch_related("items__product")` on the outer queryset |
  | `GenericRelation` / `GenericForeignKey` | `prefetch_related(...)` (GFK: low confidence) |
  | the relation is filtered per row (`order.items.filter(...)`) | a `Prefetch(..., queryset=..., to_attr=...)` hint |
  | loop in a template, generic `ListView` (`model = Order`) | diff on the `render()` queryset / adds `queryset = ...` to the view |
  | DRF serializer reading a relation | targets the view's `queryset` / `get_queryset()` |

  The fix is inserted before ordering and slicing, replaces a bare `.all()`, or extends an existing
  `prefetch_related(...)`. When the queryset cannot be located you get a code hint with lower
  confidence.
- **EXPLAIN** on any captured `SELECT`, run on `connections[alias]` inside a transaction that is
  always rolled back (and `READ ONLY` on PostgreSQL). Plans are analysed for full scans, temp sorts
  and spills, with `CREATE INDEX` suggestions. Works on SQLite, PostgreSQL and MySQL.
- **Flamegraphs**: add `?_redline=profile` to any URL (or send `X-Redline-Profile: 1`) to profile
  that request with pyinstrument (needs the `profile` extra).
- **Load-test correlation**: an `X-Redline-Run: <id>;step=<n>` request header tags the trace.

## Settings

Everything is optional. `REDLINE` keys are case-insensitive, and unknown keys fail the system check
with a "did you mean" hint.

```python
REDLINE = {
    "ENABLED": True,
    "SAMPLE_RATE": 1.0,               # default 1.0 with DEBUG=True, 0.1 with DEBUG=False
    "ALWAYS_KEEP_SLOWER_THAN_MS": 500,
    "ALWAYS_KEEP_ERRORS": True,
    "IGNORE_PATHS": ["/static/", "/media/", "/favicon.ico", "^/health"],  # "^" = regex
    "STORAGE_PATH": BASE_DIR / ".redline/redline.db",  # the default; str or pathlib.Path
    "N_PLUS_ONE_THRESHOLD": 5,
    "SLOW_QUERY_MS": 100,
    "TOO_MANY_QUERIES": 50,
    "PROFILE": "opt_in",              # off | opt_in | sampled | all
    "EXPLAIN_ANALYZE": False,         # ANALYZE executes the query: off by default
    "MAX_ROWS": 10_000, "MAX_AGE_DAYS": 7,  # 0 or None disables a retention rule
    "MAX_SPANS_PER_TRACE": 2_000,     # beyond it spans are counted, not stored
    "PERMISSION": "myproject.redline.can_view",   # Django-only, see below
    "STORE": "myproject.redline.make_store",      # Django-only: a custom Store (optional)
}
```

The full list is in the settings reference of the docs (`redline_core.conf.Settings`).
`PROJECT_ROOT` defaults to `BASE_DIR`. It decides which stack frames count as "your code".

## Security

- **Access**: staff only by default (`user.is_active and user.is_staff`). Override it with
  `REDLINE["PERMISSION"]`, a callable `(request) -> bool` or its dotted path. Anonymous visitors to
  the page are redirected to the admin login. The API answers `403` JSON. Redline's views also
  enforce `ALLOWED_HOSTS` (against DNS rebinding) and the page sends `X-Frame-Options: DENY`.
- **CSRF**: the JSON API runs Django's own CSRF check on every request, even without
  `CsrfViewMiddleware`. Mutating calls (`DELETE` traces, `POST api/explain`) need the token in the
  CSRF header (`X-CSRFToken` by default). On HTTPS the Origin/Referer checks also apply. The page
  embeds a fresh token in `<meta name="redline-csrf">`, so this also works with
  `CSRF_COOKIE_HTTPONLY` / `CSRF_USE_SESSIONS`. Failures are a JSON `403` `csrf_failed`.
- **Redaction**: `Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key` headers and
  password/token/secret-like keys in query strings, bodies and SQL params are masked before anything
  is stored.
- Requests to Redline's own URLs are never recorded. Its storage is a separate SQLite file, never a
  Django connection.

## Your own spans

```python
from django_redline import span

@span("pricing.compute")
def compute(order): ...

with span("render-pdf", pages=3):
    ...
```

## Operations

- `manage.py redline_clear [--noinput]` deletes every trace.
- `manage.py redline_prune [--max-age-days N] [--max-rows N]` applies retention now. The writer also
  prunes as it goes.
- System checks (`manage.py check`):

  | ID | Problem |
  |---|---|
  | `E001` | invalid `REDLINE` |
  | `E002` | storage not writable |
  | `W001` | middleware missing |
  | `W002` | `GZipMiddleware` inside Redline |
  | `W003` | Redline listed after middleware it should time |
  | `W004` | URLs not mounted |
  | `W005` | `DEBUG=False` with `SAMPLE_RATE=1.0` |

- Writes happen on a background thread through a bounded queue. Under overload, traces are dropped
  and counted rather than slowing requests.

## How it hooks in

- The middleware is sync and async capable. SQL goes through `connection.execute_wrapper` on every
  connection. Cache spans come from wrapping the backends that `caches` creates. Template spans come
  from `Template.render`, and the view span from `BaseHandler.make_view_atomic`.
- Each hook is a single `ContextVar` lookup when nothing is being recorded.
- Queries run from library code (generic views, DRF, lazily rendered `TemplateResponse`s) are
  attributed to the project view that handled the request.

Supports Django 5.2, 6.0 and 6.1 on Python 3.12+. A runnable demo is in
[`examples/django`](../../examples/django).

## License

MIT
