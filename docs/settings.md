# Settings reference

[← Index](index.md)

Every setting, its type, default and effect. They are defined once, in
`redline_core.conf.Settings`, and shared by every adapter. Each adapter only chooses *how
you pass them*, and settings can also come from a [preset, a config file and `REDLINE_*`
environment variables](configuration.md), which this page's defaults sit underneath.

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

=== "Flask"

    ```python
    app.config["REDLINE"] = {"sample_rate": 0.1, "slow_query_ms": 50}
    Redline(app, engine=engine)           # or Redline(app, settings={...})
    ```

=== "Litestar"

    ```python
    RedlinePlugin(engine=engine, settings={"sample_rate": 0.1, "slow_query_ms": 50})
    ```

=== "redline.toml"

    ```toml
    sample_rate = 0.1
    slow_query_ms = 50
    ```

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
| `storage_url` | `postgresql://` URL or `None` | `None` | Store traces in this shared PostgreSQL database instead of the SQLite file, so several workers and hosts write one store. Needs `redline-core[postgres]`. The password is masked wherever settings are shown. See [Storage: a shared PostgreSQL store](storage.md#a-shared-postgresql-store) |
| `project_root` | `str`, `os.PathLike` or `None` | `None` | Directory that marks "your code" for [callsite capture](how-it-works.md#callsites). `None` means the working directory. **Django default:** `BASE_DIR`. Also where [`redline.toml`](configuration.md#the-config-file) is looked for |
| `preset` | `"dev"`, `"shared-dev"`, `"staging"` or `"production"` | `"dev"` | A bundle of defaults. Explicit settings override it. See [Configuration](configuration.md#presets) |
| `release` | `str` or `None` | `None` (automatic) | Name stamped on each trace for [History](history.md#releases). Automatic: `REDLINE_RELEASE`, then `GIT_SHA` / `SOURCE_VERSION` / `RENDER_GIT_COMMIT` / `HEROKU_SLUG_COMMIT`, then `git rev-parse --short HEAD` in `project_root` (once, one-second timeout), else none |

## Sampling: what gets stored

See [How it works: sampling](how-it-works.md#sampling).

| Setting | Type | Default | Description |
|---|---|---|---|
| `sample_rate` | `float`, 0 to 1 | `1.0` | Fraction of requests kept at random. **Django default:** `1.0` when `DEBUG` is true and **`0.1` when `DEBUG` is false** (and the check `django_redline.W005` fires if you set `1.0` in production) |
| `always_keep_slower_than_ms` | `float`, 0 or more | `500` | A request at least this slow is kept regardless of the dice. `0` keeps everything |
| `always_keep_errors` | `bool` | `True` | Keep requests that returned status 500 or above, raised an unhandled exception, or contained an observed SQL/connection failure, regardless of the dice. A **4xx is never an error**, even when it was raised as an exception (Django's `Http404`, `PermissionDenied`): those traces have `error: null` and are kept only by the dice or the slow rule |
| `ignore_paths` | list of `str` | `["/static/", "/media/", "/favicon.ico"]` | Paths never recorded. An entry starting with `^` is a **regular expression** (matched with `re.search` on the path); anything else is a **path prefix**. Redline's own mount is always added |

## Capture and redaction

| Setting | Type | Default | Description |
|---|---|---|---|
| `capture_request_body` | `bool` | `True` | Store request bodies (redacted, capped). When false, bodies are not read at all |
| `capture_response_body` | `bool` | `True` | Store response bodies (redacted, capped). Streaming and file responses are never captured |
| `max_body_bytes` | `int`, 0 or more | `20000` | Cap for a stored body. Longer bodies are cut and flagged `body_truncated`. Masking runs **before** the cut |
| `redact_headers` | list of `str` | `["authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization"]` | Header names (case-insensitive) whose values are replaced with `[redacted]` |
| `redact_keys` | list of regex | `["(?i)pass(word)?\|secret\|token\|api[_-]?key\|(?<![a-z])auth(orization)?(?![a-z])\|session\|csrf\|card\|cvv\|ssn\|private[_-]?key"]` | Patterns matched with `re.search` against **key names** in query strings, form and JSON bodies (at any depth), outbound URLs, SQL parameter column names, log messages (`key=value` pairs) and the names of exception locals. A match masks the value. `auth` and `authorization` match only as a whole word, so `author`, `author_id`, `authenticated` and `oauth_provider` stay visible |
| `capture_logs` | `bool` | `True` | Keep the log records emitted during a request. See [Logs and exceptions](logs-and-exceptions.md) |
| `log_level` | `str` (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`; case-insensitive) | `"INFO"` | An extra minimum level for records stored in a trace. Capture follows your application's own logger levels (Redline never adds a root handler or changes a level), so a record your logging configuration drops is never captured; `log_level` can only raise the bar further |
| `max_logs_per_trace` | `int` | `200` | Cap per trace; extras are counted, not stored |
| `capture_locals` | `bool` | `True` | Keep the local variables of project frames in an exception (names matching `redact_keys` are masked). See [Logs and exceptions](logs-and-exceptions.md#exceptions-with-locals) |
| `capture_error_details` | `bool` | `True` | Capture exception messages and formatted tracebacks. When false, errors retain type/codes without formatting message or traceback text; exception locals are also disabled. SQL messages additionally require SQL parameter capture for that statement. Shared-dev and production default to false |

## Detectors

See [N+1 detection](n-plus-one.md).

| Setting | Type | Default | Minimum | Description |
|---|---|---|---|---|
| `n_plus_one_threshold` | `int` | `5` | 2 | The same read from the same line at least this many times is an N+1 |
| `duplicate_threshold` | `int` | `2` | 2 | Identical SQL and params at least this many times is a duplicate |
| `sql_retry_storm_threshold` | `int` | `3` | 2 | Attempts after a transient SQL failure (same statement and callsite) before a `sql_retry_storm` finding |
| `slow_query_ms` | `float` | `100` | 0 | A query at least this slow is a slow query. `0` flags every query. It also feeds the N+1 and duplicate severity rules |
| `too_many_queries` | `int` | `50` | 0 | A request with at least this many queries is over budget. `0` disables the check |
| `blocking_ms` | `float` | `20` | 0 | A stall of the event loop of at least this long is reported. See [Blocking in async](async-blocking.md) |
| `large_result_rows` | `int` | `1000` | 0 | A SELECT returning at least this many rows is a [large result](result-size.md) |
| `unbounded_rows` | `int` | `200` | 0 | A SELECT with no `LIMIT` returning at least this many rows is [unbounded](result-size.md#unbounded-query) |
| `slow_serializer_ms` | `float` | `50` | 0 | A [serializer](serializers.md) at least this slow is reported |
| `long_transaction_ms` | `float` | `200` | 0 | A [transaction](transactions.md) open at least this long is reported |
| `slow_external_ms` | `float` | `500` | 0 | An [outbound call](external-calls.md) at least this slow is reported |

Two more switches belong here. `detect_blocking` (`bool`, default `True`) runs the
event-loop lag monitor in async adapters, only while a request is in flight; set it false to
remove it entirely.

## Profiling

See [Flamegraphs](flamegraphs.md). Needs `redline-core[profile]`.

| Setting | Type | Default | Description |
|---|---|---|---|
| `profile` | `"off"`, `"opt_in"`, `"sampled"`, `"all"` | `"opt_in"` (`production` preset: `"off"`) | Who gets profiled. Anything else is an error. In `opt_in`/`sampled`, `?_redline=profile` / `X-Redline-Profile: 1` count only in framework debug mode or from a request with valid Redline credentials |
| `profile_sample_rate` | `float`, 0 to 1 | `0.0` | In `"sampled"` mode, the fraction of untriggered requests to profile |

## EXPLAIN

| Setting | Type | Default | Description |
|---|---|---|---|
| `explain_analyze` | `bool` | `False` | Allow `EXPLAIN ANALYZE`, which **executes** the query. Off: `analyze: true` is refused with `403 analyze_disabled`. See [EXPLAIN](explain.md#safety) |

## Developer experience

| Setting | Type | Default | Description |
|---|---|---|---|
| `editor` | `"vscode"`, `"cursor"`, `"pycharm"`, `"idea"`, `"sublime"`, `"none"`, or a template string | `"vscode"` | Where callsite links open. A template contains `{file}` (absolute path) and `{line}`. See [Open in editor](the-ui.md#open-in-editor) |
| `server_timing` | `"auto"` or `bool` | `"auto"` | Add a `Server-Timing` header (trace id, query count and time) to recorded responses. `auto` means on when the framework is in debug mode. See [Server-Timing and the badge](server-timing-badge.md) |
| `badge` | `"auto"` or `bool` | `"auto"` | Inject the floating badge into HTML responses. `auto` means on when the framework is in debug mode (FastAPI, Flask and Litestar: the app's `debug` flag) |
| `load_from_ui` | `bool` | `False` | Allow starting a [load run](load-testing.md#starting-a-run-from-the-ui) from the UI (`POST api/runs`). Needs `redline-core[load]`. Off whenever `read_only` is on or the `load` feature is off |
| `checks_from_ui` | `"auto"` or `bool` | `"auto"` | Allow starting [refactor-check](refactor-check.md) baseline and compare runs from the UI (`POST api/checks/runs`). `auto` means on only while the framework is in debug mode; `true` and `false` force it. Needs `redline-core[load]` (httpx). Off whenever `read_only` is on or the `checks` feature is off; the `production` preset sets it to `false`. Reading suites and reports is not affected |

## Storage and retention

See [Storage](storage.md).

| Setting | Type | Default | Description |
|---|---|---|---|
| `max_rows` | `int` (0 or more) or `None` | `10000` | Keep at most this many traces; the oldest beyond it are pruned. **`0` or `None` disables the row cap** |
| `max_age_days` | `int` (0 or more) or `None` | `7` | Prune traces older than this. **`0` or `None` disables age-based pruning** |
| `max_spans_per_trace` | `int` (0 or more) or `None` | `2000` | Spans stored per request. Beyond it spans are **counted, not stored**; query totals are retained and the trace is marked `truncated`. Bounded overflow groups can preserve detector evidence, but byte limits may discard those groups too. Middleware/view spans bypass the count cap only while within the byte budget. **`0` or `None` disables this count cap**, not byte limits. See [Performance](performance.md#memory) |
| `history_retention_days` | `int` (0 or more) or `None` | `90` | How long hourly rollups, the issue index, releases and load runs are kept, independent of `max_age_days`. **`0` or `None` keeps them forever**. See [History](history.md#retention) |
| `writer_queue_size` | `int` | `1000` (minimum 1) | Bound of the in-memory queue between requests and the writer thread. When full, new traces are **dropped and counted** (`storage.dropped` in `api/meta`). SQLite writer failures also add lost batch traces to this counter |

For the optional retention/span count limits above, `0` and `None` mean "no limit";
negative values are errors. `writer_queue_size` always requires an integer of at least 1. To delete everything use **Clear all**, `DELETE api/traces` or
`manage.py redline_clear`, never a zero.

## Capture byte budgets

These positive integer settings complement span/log counts and body caps:

| Setting | Minimum | Default | Production preset | Scope |
|---|---|---|---|---|
| `max_sql_bytes` | `256` | `65536` (64 KiB) | `16384` (16 KiB) | SQL text inspected/captured per execution; oversized text is incomplete evidence |
| `max_span_data_bytes` | `256` | `32768` (32 KiB) | `8192` (8 KiB) | Bounded JSON-safe data attached to an individual span |
| `max_tag_bytes` | `32` | `2048` (2 KiB) | `2048` (2 KiB) | Individual captured tag values; ordinary tags also have item/key limits |
| `max_trace_bytes` | `4096` | `1048576` (1 MiB) | `262144` (256 KiB) | Retained-span byte budget during recording and final serialized trace budget before saving |

These budgets cannot be disabled with `0` or `None`. They apply independently of the
older optional count/retention limits. Bounded capture limits traversal depth and total
items, handles cycles, shortens UTF-8 text safely, and substitutes unsupported application
objects without invoking their `str()`/`repr()` methods. This is an observation limit;
it does not constrain the application's original object allocations or database payload.

Per-value conversion uses bounded compact ASCII JSON; the final trace check measures
UTF-8 JSON with the storage serializer's default spacing, checking both profile-included
and profile-lookup shapes without arbitrary object conversion. It is a recording payload
budget, not a cap on SQLite indexes, history tables, WAL files or total disk space.

The production preset also reduces `writer_queue_size` to `64` and
`max_spans_per_trace` to `500`. Explicit settings override these defaults.

Truncation and dropped evidence must remain visible in a trace. A shortened statement
or omitted span cannot prove a query was safe or a request contained no failures.
Recorded SQL/connection error counts survive discarded span evidence. Altered captures
set `truncated.capture_limits` and `truncated.incomplete_evidence`. A total-size reduction
can set `reason = "trace_budget"` and dropped-span/issue counts. Incomplete SQL spans
withhold parameters and disable EXPLAIN. A privacy-processing failure produces only safe
minimal metadata with `reason = "privacy_failure"`; failure counts then live under
`truncated`, with no application tags or raw request content. See [HTTP API](http-api.md#capture-budgets-and-incomplete-evidence). A queue-length
limit also does not set a total process-memory bound: queued records, active requests,
analysis and the application have separate overhead. Measure the full process under load.

## Access control

Who may open the UI and API, and how. The policies, recipes and reasoning are on
[Access control](access-control.md); the table is the reference. Secrets (`token`,
`secret_key`, `basic_users`) are shown as `***` by `GET api/config` and `redline config`.

| Setting | Type | Default | Description |
|---|---|---|---|
| `access` | `"local"`, `"token"`, `"basic"`, `"framework"`, `"custom"` or `"open"` | `"local"` (Django: `"framework"`, superusers) | The access policy. `"open"` is refused (`403 open_refused`) unless `REDLINE_ALLOW_OPEN=1` is set too. A native hook (non-empty FastAPI `dependencies=` / Litestar `guards=`, a Flask `permission=`) implies `"framework"` when you did not set `access` yourself. **Not set at all** (FastAPI, Flask, Litestar): `local` applies only in framework debug mode; otherwise every Redline URL answers `404` |
| `trust_unix_socket` | `bool` | `False` | Under `local`, count a unix-socket peer (no client address) as loopback. Off because a same-host reverse proxy is the usual such peer |
| `cookie_secure` | `"auto"`, `True` or `False` | `"auto"` | `Secure` attribute of the login cookie. `"auto"`: when the request is https (`X-Forwarded-Proto` counts only from `trusted_proxies`) |
| `access_check` | callable `(request_info) -> bool`, or a dotted path (`pkg.mod:fn` or `pkg.mod.fn`) | `None` | For `access = "custom"`. An exception counts as a refusal and is logged |
| `allowed_ips` | list of IPs or CIDRs (v4 or v6) | `[]` | With `local`: clients allowed **besides** loopback. With every other policy: when non-empty the client **must also** be in it (loopback is not implicit) |
| `allowed_hosts` | list of `str` | `[]` | Extra `Host` names accepted by `local`'s DNS-rebinding check (ports ignored). Also accepted as targets of UI-started load runs |
| `trusted_proxies` | list of IPs or CIDRs | `[]` | Only requests whose TCP peer is in this list have `X-Forwarded-For`, `-Proto` and `-Host` believed. A loopback peer sending forwarding headers without being listed is refused under `local` |
| `token` | `str` (secret) | `None` | The token for `access = "token"`: one string, no whitespace. Outside the `dev` preset it must be at least 24 characters. Prefer `REDLINE_TOKEN` |
| `token_file` | path | `None` | Read the token from a file once at startup (stripped). Not together with `token` |
| `basic_users` | table of user to hash (secret) | `{}` | For `access = "basic"`; hashes made by `redline hash-password` (`pbkdf2_sha256$...`). User names may not contain `:` |
| `secret_key` | `str`, 16 characters or more (secret) | `None` | Signs the login cookie. Unset: a random key per process, so with several workers a login only works on the worker that issued it, and every restart logs everyone out |
| `read_only` | `bool` | `False` | Refuse every non-GET `api/` call except `api/explain`, `api/login` and `api/logout` with `403 read_only`; `POST api/explain` with `analyze` is refused too |
| `features` | table of switch to `bool` | all `True` | Turn individual features off: `explain`, `profile`, `replay`, `replay_writes`, `snapshot`, `load`, `checks`, `logs`, `locals`, `sse`. An unknown name is an error. Merged per key across layers |
| `ui_enabled` | `bool` | `True` | `False`: every Redline URL answers `404` while capture continues |
| `mount_path` | `str` | `None` | Where FastAPI (`prefix`), Flask (`url_prefix`) and Litestar (`path`) serve the UI when that argument is not given; those arguments now default to `None`, and the final default is `/_redline`. Django ignores it (the URLconf decides). Also the default for `redline load --ui-url` |
| `session_ttl_s` | `int`, 1 or more | `43200` | Login cookie lifetime in seconds |
| `max_login_attempts` | `int`, 1 or more | `10` | Failed credential attempts per client IP per 300 seconds before `429 too_many_requests` |
| `base_url` | http(s) URL | `None` | Target of load runs and refactor checks started from the UI. Unset: `127.0.0.1` on the port the server listens on (ASGI `scope["server"]`, WSGI `SERVER_PORT`), never the `Host` header; required when that is unknown (a unix socket). `GET api/config` masks userinfo in it |

## Presets

`preset` only sets **defaults**. Anything you set elsewhere overrides it. The table is
exactly what `redline_core.conf.PRESETS` applies:

| | `dev` (default) | `shared-dev` | `staging` | `production` |
|---|---|---|---|---|
| `sample_rate` | `1.0` | `1.0` | `0.25` | `0.05` |
| `capture_request_body` / `capture_response_body` | on | off | on | off |
| `capture_sql_params` | on | off | off | off |
| `capture_locals` | on | off | off | off |
| `capture_error_details` / `capture_logs` | on | off | on | off |
| `log_level` | `INFO` | `WARNING` | `INFO` | `WARNING` |
| `access` | `local` | `token` | `token` | `token` |
| `read_only` | off | on | off | on |
| `badge` / `server_timing` | `auto` | `auto` | `auto` | off |
| `load_from_ui` | off | off | off | off |
| `checks_from_ui` / `analysis_from_ui` | `auto` | off | `auto` | off |
| `explain_analyze` | off | off | off | off |
| `profile` | `off` | `off` | `off` | `off` |
| `writer_queue_size` | `1000` | `1000` | `1000` | `64` |
| `max_spans_per_trace` | `2000` | `2000` | `2000` | `500` |
| `max_sql_bytes` | `65536` | `65536` | `65536` | `16384` |
| `max_span_data_bytes` | `32768` | `32768` | `32768` | `8192` |
| `max_tag_bytes` | `2048` | `2048` | `2048` | `2048` |
| `max_trace_bytes` | `1048576` | `1048576` | `1048576` | `262144` |

`always_keep_errors` remains on in all presets. Shared-dev defaults retention to three
days. Shared-dev disables replay and replay writes. Staging disables replay writes.
Production disables the replay, replay writes, load, checks, explain and profile feature
gates as well as their UI opt-ins. Stored traces and their recorded analysis remain
available to authorized readers. Explicit settings override preset defaults; review
`redline config` after layering configuration.

Outside `dev` a token must be at least 24 characters, and `access = "token"` without a
`token` or `token_file` is always an error. Slow requests are kept in every preset
(`always_keep_slower_than_ms`, default `500`).

See [Configuration](configuration.md#presets) and [Production](production.md#start-with-recording-and-stored-data-inspection).

## Where a value comes from

Layers, last one wins: built-in defaults, the adapter's defaults, preset, config file
(`redline.toml` or `[tool.redline]`), `REDLINE_<SETTING>` environment variables, then the
framework's own settings. `redline config` prints the result and the layer of every value. See
[Configuration](configuration.md#layers-and-precedence).

## Django-only

| Setting | Type | Default | Description |
|---|---|---|---|
| `PERMISSION` | callable `(request) -> bool` or a dotted path to one | active superusers (`user.is_active and user.is_superuser`; staff before 0.2, kept as `django_redline.conf.staff_permission`). Used when `access = "framework"` | Who may open the UI and the API. See [Django: access control](django.md#access-control) |
| `STORE` | a `Store` instance, a callable (or class) taking the core `Settings` and returning one, or a dotted path to either | `None` (`SQLiteStore.from_settings(settings)`) | Where traces are kept: Django's counterpart of FastAPI's `install(store=...)`. See [Extending: the Store protocol](extending.md#the-store-protocol) |

`PERMISSION` and `STORE` are read by the Django adapter before the rest is handed to the
core, so they are not core settings and the other adapters do not accept them in `settings`.
Their counterparts are FastAPI's `dependencies` and `store=`, Flask's `permission=` and
`store=`, and Litestar's `guards=` and `store=`.

## Adapter defaults at a glance

| | Django | FastAPI | Flask | Litestar |
|---|---|---|---|---|
| Passed as | `settings.REDLINE` dict | `install(settings=...)` | `app.config["REDLINE"]` or `Redline(settings=...)` | `RedlinePlugin(settings=...)` |
| `sample_rate` | `1.0` with `DEBUG`, else `0.1` | `1.0` | `1.0` | `1.0` |
| `storage_path` | `<BASE_DIR>/.redline/redline.db` | `.redline/redline.db` (working directory) | same as FastAPI | same as FastAPI |
| `project_root` | `BASE_DIR` | working directory | working directory | working directory |
| Mount prefix | wherever you `include()` the URLs (e.g. `/redline/`) | `prefix`, else `mount_path`, else `/_redline` | `url_prefix`, else `mount_path`, else `/_redline` | `path`, else `mount_path`, else `/_redline` |
| Access | `PERMISSION`, default superusers (`framework`) | `local` (loopback only, debug mode only while `access` is unset); `dependencies=` implies `framework` | `local` (same); `permission=` implies `framework` | `local` (same); `guards=` implies `framework` |

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


## v0.3 analysis settings

`analysis_from_ui` accepts `"auto"`, `true` or `false`. Auto enables source scans and
synthetic detector samples only in framework debug mode; the production preset sets it
to false. `read_only` still refuses these POST operations. Detector threshold settings,
model-count semantics and explicit cache-decode instrumentation are documented in
[Performance workflows](performance-workflows.md#additional-runtime-findings). Preview
controls do not change the server's effective configuration.


## v0.4 database, privacy and job settings

These settings apply to the common recorder. Adapters resolve file/environment/framework
layers as described above. The [real-world guide](real-world-workflows.md) explains the
behavior and limitations; the [production recipe](production.md) deliberately disables
active tools and sensitive capture for its initial canary.

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `capture_sql_params` | `bool` or `"reads_only"` | `True` (`False` in staging/shared-dev/production) | `False` withholds parameters and inline SQL literals, SQL source/fix snippets and potentially value-bearing SQL error messages. EXPLAIN is unavailable for that recording. `reads_only` retains values only for conservatively recognized reads |
| `instrument_engines` | `"explicit"`, `"all"` or `"flask_sqlalchemy"` | `"explicit"` | `all` enables process-level SQLAlchemy discovery for active opted-in recorders. Explicit engine registration is still supported; Flask-SQLAlchemy discovery remains available. It does not wrap arbitrary raw DBAPI connections |
| `session_expected` | mapping of option name to SQL value spelling | `{"NOCOUNT": "OFF"}` | Baseline for observed session switches at pool checkin. SQL Server also observes isolation level, `IDENTITY_INSERT`, date/language/timeout/deadlock switches; PostgreSQL and MySQL observe session options, `set_config(x, y, false)` and MySQL user variables. Unknown changes are not invented. Generic string-valued options are compared by digest, so use the expected SQL value spelling |
| `session_expected_context` | list of strings | `[]` | SQL Server `sp_set_session_context` keys the application is expected to use. A remaining unnamed key is reported; an expected key is reported when it was set with `@read_only = 0` (lower severity). Values are never stored |
| `redact_profiles` | list of names | `[]` | Built-in pattern sets: `credentials`, `pii`, `health`, `payment`. They are starting points, not a privacy/compliance guarantee |
| `redact_tables` | list of regexes | `[]` | Additional table-name patterns for SQL parameter masking |
| `redact_routes` | mapping of `METHOD route-template` glob to mode | `{}` | `values` preserves JSON structure and masks values; `body` removes body contents. Also restricts that recording's SQL/query values, logs and exception/source detail before persistence |
| `capture_loggers` | list of logger-name globs | `[]` | Empty means no additional allowlist. A nonempty list captures only matching logger names; application level and `log_level` still apply |
| `ignore_loggers` | list of logger-name globs | `[]` | Matching loggers are excluded even if the allowlist matches |
| `request_id_headers` | list of header names | `["X-Request-ID", "X-Correlation-ID", "traceparent"]` | First configured request/response identifier becomes `request_id`; response takes precedence at completion. A valid `traceparent` also fills OTel trace/parent ids. Explicit header/key redaction still applies |
| `user_resolver` | synchronous Python callable or `None` | `None` | Called late with a framework request; return only an identifier string/integer. It is not a TOML string import path. Errors become a resolver-error tag rather than breaking the response |
| `storage_role` | simple role name | `"web"` | Substitution for `{role}` in `storage_path`. Use `worker` explicitly in a task worker; it is not inferred from Celery |
| `extra_stores` | list of existing file paths, at most 8 | `[]` | Read-only secondary request/job/profile browsing. It does not combine aggregates/history or permit secondary mutations/database execution |
| `allow_storage_in_repo` | `bool` | `False` | Non-dev presets refuse an in-repository store unless explicitly overridden. Prefer a private absolute path outside the checkout |

Nested `[redact]` configuration is equivalent to the flat fields:

```toml
[redact]
profiles = ["credentials", "pii", "health"]
tables = ["(?i)medical|clinical"]
routes = { "POST /private/*" = "values" }
```

Use either the nested spelling or its flat equivalent, not both for the same setting.
Ordinary key/header lists retain their replacement semantics. Enabling a profile does
not make arbitrary values in logs or exception text recognizable.

`storage_path` supports `{role}`, `{hostname}` and `{pid}`. Use an application/environment
prefix too, such as `/var/lib/redline/orders-production/{role}.db`. Templates do not make
remote filesystem storage safe and do not create per-application authorization.

### Production versus shared development

`shared-dev` disables request/response bodies, locals and SQL values; it enables token
access, read-only behavior, WARNING-level log capture and three-day trace retention. UI
load/check/source analysis and replay are off by default. It is useful for shared staging
work but is not a substitute for the stricter [production recipe](production.md).

An explicit setting overrides a preset, including sensitive capture switches. Inspect
`GET api/config` and recording health in the deployed process rather than assuming an
environment variable or configuration file was loaded by every worker.

### Job helpers and adapter options

`job(name, recorder=..., task_id=..., queue=..., attempt=...)`, `stage(name)` and
`row_outcome(status, reason=...)` are Python APIs, not settings. Pass the intended recorder
in a multi-application/worker process. `instrument_celery(app, recorder, task_filter=...)`
installs optional task signals; arguments/results are not captured.

Flask's `Redline(..., recorder=outer.recorder, mode="nested")` is an adapter option. The
shared recorder owns settings/storage; do not also pass a separate settings/store mapping.
An active outer recording is reused, and a standalone request can still create its own
recording through the same recorder.
