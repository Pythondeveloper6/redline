# Settings reference

[← Index](index.md)

Every setting, its type, default and effect. They are defined once, in
`redline_core.conf.Settings`, and shared by both adapters. Each adapter only chooses *how
you pass them*:

=== "Django"

    ```python
    # settings.py
    REDLINE = {
        "SAMPLE_RATE": 0.1,
        "SLOW_QUERY_MS": 50,
        "PERMISSION": "myproject.redline.can_view",   # Django-only
    }
    ```

=== "FastAPI"

    ```python
    install(app, engine=engine, settings={
        "sample_rate": 0.1,
        "slow_query_ms": 50,
    })
    ```

    or pass a `redline_core.Settings` instance.

## Rules that apply to every setting

- **Keys are case-insensitive.** `SAMPLE_RATE` and `sample_rate` are the same. The tables
  below use the lower-case names (Django: write them upper-case by convention).
- **Unknown keys are errors**, with a suggestion: `SettingsError: Unknown Redline
  setting(s): 'sampel_rate' (did you mean 'sample_rate'?). Valid settings: ...`. In
  Django this surfaces as `ImproperlyConfigured` and as the system check
  `django_redline.E001`.
- **Types are checked.** A string where an integer belongs, a negative number, or a
  `bool` where a number belongs is rejected. A float or int is fine for any number setting.
- **List settings** (`ignore_paths`, `redact_headers`, `redact_keys`) accept a list, tuple
  or a single string (treated as a one-element list).
- **Regular expressions are compiled at load time**, so an invalid pattern fails at
  startup, not on the first request.
- `Settings` is **immutable**. `settings.replace(...)` returns a changed copy.

!!! warning "A list you set replaces the default, it does not extend it"

    Setting `ignore_paths` or `redact_headers` or `redact_keys` discards the built-in
    list. If you set `"IGNORE_PATHS": ["^/health"]`, then `/static/`, `/media/` and
    `/favicon.ico` are **no longer ignored**. Include the defaults you want to keep. (The
    adapters still add Redline's own mount for you.) The same holds for `redact_keys`:
    overriding it with one pattern removes password, token and card masking unless
    your pattern includes them. See [Security](security.md#redaction).

## General

| Setting | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `True` | Master switch. When false nothing is recorded or stored. Django: the middleware stays installed but every request passes straight through. FastAPI: `install()` adds no middleware and no SQLAlchemy listeners. The UI still serves stored traces |
| `storage_path` | `str` or `os.PathLike` (converted to `str`) | `".redline/redline.db"` | The SQLite file. Parent directories are created. `":memory:"` works. **Django default:** `<BASE_DIR>/.redline/redline.db`. See [Storage](storage.md) |
| `project_root` | `str`, `os.PathLike` or `None` | `None` | Directory that marks "your code" for [callsite capture](how-it-works.md#callsites). `None` means the working directory. **Django default:** `BASE_DIR` |

## Sampling: what gets stored

See [How it works: sampling](how-it-works.md#sampling).

| Setting | Type | Default | Description |
|---|---|---|---|
| `sample_rate` | `float`, 0 to 1 | `1.0` | Fraction of requests kept at random. **Django default:** `1.0` when `DEBUG` is true and **`0.1` when `DEBUG` is false** (and the check `django_redline.W005` fires if you set `1.0` in production) |
| `always_keep_slower_than_ms` | `float`, 0 or more | `500` | A request at least this slow is kept regardless of the dice. `0` keeps everything |
| `always_keep_errors` | `bool` | `True` | Keep requests that returned status 500 or above, or raised an unhandled exception, regardless of the dice. A **4xx is never an error**, even when it was raised as an exception (Django's `Http404`, `PermissionDenied`): those traces have `error: null` and are kept only by the dice or the slow rule |
| `ignore_paths` | list of `str` | `["/static/", "/media/", "/favicon.ico"]` | Paths never recorded. An entry starting with `^` is a **regular expression** (matched with `re.search` on the path); anything else is a **path prefix**. Redline's own mount is always added |

## Capture and redaction

| Setting | Type | Default | Description |
|---|---|---|---|
| `capture_request_body` | `bool` | `True` | Store request bodies (redacted, capped). When false, bodies are not read at all |
| `capture_response_body` | `bool` | `True` | Store response bodies (redacted, capped). Streaming and file responses are never captured |
| `max_body_bytes` | `int`, 0 or more | `20000` | Cap for a stored body. Longer bodies are cut and flagged `body_truncated`. Masking runs **before** the cut |
| `redact_headers` | list of `str` | `["authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization"]` | Header names (case-insensitive) whose values are replaced with `[redacted]` |
| `redact_keys` | list of regex | `["(?i)pass(word)?\|secret\|token\|api[_-]?key\|auth(?!or)\|session\|csrf\|card\|cvv\|ssn"]` | Patterns matched with `re.search` against **key names** in query strings, form and JSON bodies (at any depth), outbound URLs, and SQL parameter column names. A match masks the value. `auth(?!or)` means `author` and `author_id` stay visible |

## Detectors

See [N+1 detection](n-plus-one.md).

| Setting | Type | Default | Minimum | Description |
|---|---|---|---|---|
| `n_plus_one_threshold` | `int` | `5` | 2 | The same read from the same line at least this many times is an N+1 |
| `duplicate_threshold` | `int` | `2` | 2 | Identical SQL and params at least this many times is a duplicate |
| `slow_query_ms` | `float` | `100` | 0 | A query at least this slow is a slow query. `0` flags every query. It also feeds the N+1 and duplicate severity rules |
| `too_many_queries` | `int` | `50` | 0 | A request with at least this many queries is over budget. `0` disables the check |

## Profiling

See [Flamegraphs](flamegraphs.md). Needs `redline-core[profile]`.

| Setting | Type | Default | Description |
|---|---|---|---|
| `profile` | `"off"`, `"opt_in"`, `"sampled"`, `"all"` | `"opt_in"` | Who gets profiled. Anything else is an error |
| `profile_sample_rate` | `float`, 0 to 1 | `0.0` | In `"sampled"` mode, the fraction of untriggered requests to profile |

## EXPLAIN

| Setting | Type | Default | Description |
|---|---|---|---|
| `explain_analyze` | `bool` | `False` | Allow `EXPLAIN ANALYZE`, which **executes** the query. Off: `analyze: true` is refused with `403 analyze_disabled`. See [EXPLAIN](explain.md#safety) |

## Storage and retention

See [Storage](storage.md).

| Setting | Type | Default | Description |
|---|---|---|---|
| `max_rows` | `int` (0 or more) or `None` | `10000` | Keep at most this many traces; the oldest beyond it are pruned. **`0` or `None` disables the row cap** |
| `max_age_days` | `int` (0 or more) or `None` | `7` | Prune traces older than this. **`0` or `None` disables age-based pruning** |
| `max_spans_per_trace` | `int` (0 or more) or `None` | `2000` | Spans stored per request. Beyond it spans are **counted, not stored**: `query_count`, `query_ms` and the N+1 and slow-query issues still cover every query, and the trace is marked `truncated` (the UI shows a notice). `middleware` and `view` spans are always kept. **`0` or `None` disables the cap**. See [Performance](performance.md#memory) |
| `writer_queue_size` | `int` | `1000` (minimum 1) | Bound of the in-memory queue between requests and the writer thread. When full, new traces are **dropped and counted** (`storage.dropped` in `api/meta`) |

For every limit above, `0` and `None` mean the same thing, "no limit", and a negative value
is an error. To delete everything use **Clear all**, `DELETE api/traces` or
`manage.py redline_clear`, never a zero.

## Django-only

| Setting | Type | Default | Description |
|---|---|---|---|
| `PERMISSION` | callable `(request) -> bool` or a dotted path to one | active staff users (`user.is_active and user.is_staff`) | Who may open the UI and the API. See [Django: access control](django.md#access-control) |
| `STORE` | a `Store` instance, a callable (or class) taking the core `Settings` and returning one, or a dotted path to either | `None` (`SQLiteStore.from_settings(settings)`) | Where traces are kept: Django's counterpart of FastAPI's `install(store=...)`. See [Extending: the Store protocol](extending.md#the-store-protocol) |

`PERMISSION` and `STORE` are read by the Django adapter before the rest is handed to the
core, so they are not core settings and FastAPI does not accept them in `settings` (use
`dependencies` and `install(store=...)`).

## Adapter defaults at a glance

| | Django | FastAPI |
|---|---|---|
| Passed as | `settings.REDLINE` dict | `install(settings=...)` |
| `sample_rate` | `1.0` with `DEBUG`, else `0.1` | `1.0` |
| `storage_path` | `<BASE_DIR>/.redline/redline.db` | `.redline/redline.db` (working directory) |
| `project_root` | `BASE_DIR` | working directory |
| Mount prefix | wherever you `include()` the URLs (e.g. `/redline/`) | `prefix` argument, default `/_redline` |
| Access | `PERMISSION`, default staff | `dependencies`, default loopback only |

## A complete example

=== "Django"

    ```python
    REDLINE = {
        "ENABLED": True,
        "SAMPLE_RATE": 0.1,
        "ALWAYS_KEEP_SLOWER_THAN_MS": 300,
        "ALWAYS_KEEP_ERRORS": True,
        "IGNORE_PATHS": ["/static/", "/media/", "/favicon.ico", "^/health", "^/metrics"],
        "STORAGE_PATH": str(BASE_DIR / ".redline" / "redline.db"),
        "CAPTURE_REQUEST_BODY": True,
        "CAPTURE_RESPONSE_BODY": False,
        "MAX_BODY_BYTES": 10_000,
        "N_PLUS_ONE_THRESHOLD": 5,
        "SLOW_QUERY_MS": 50,
        "TOO_MANY_QUERIES": 40,
        "PROFILE": "opt_in",
        "EXPLAIN_ANALYZE": False,
        "MAX_ROWS": 5_000,
        "MAX_AGE_DAYS": 3,
        "PERMISSION": "myproject.redline.can_view",
    }
    ```

    `STORAGE_PATH` and `PROJECT_ROOT` accept a `pathlib.Path` (any `os.PathLike`) as well
    as a string; it is converted to `str`.

=== "FastAPI"

    ```python
    install(app, engine=engine, settings={
        "sample_rate": 0.1,
        "always_keep_slower_than_ms": 300,
        "ignore_paths": ["/static/", "/favicon.ico", "^/health", "^/metrics"],
        "storage_path": "/var/lib/myapp/redline.db",
        "capture_response_body": False,
        "max_body_bytes": 10_000,
        "slow_query_ms": 50,
        "too_many_queries": 40,
        "max_rows": 5_000,
        "max_age_days": 3,
    })
    ```
