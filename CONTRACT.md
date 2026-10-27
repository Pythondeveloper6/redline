# Redline internal contract (v0.1)

This is the single source of truth shared by `redline-core`, the two adapters and the frontend.
If you need to change it, change this file in the same commit and say so in your report.

## 1. Data model (`redline_core.model`)

All times are floats. `start_ms` is milliseconds since the trace started; `duration_ms` is milliseconds. IDs are 32-char hex (`uuid4().hex`).

```python
SpanKind = Literal["sql", "cache", "http_out", "template", "middleware", "view", "custom"]

@dataclass
class Span:
    id: str
    parent_id: str | None      # None = child of the trace root
    kind: SpanKind
    name: str                  # sql: first 120 chars of normalized SQL; cache: "get key"; http_out: "GET host/path"
    start_ms: float
    duration_ms: float
    callsite: Callsite | None  # project frame that triggered it (sql/cache/http_out)
    data: dict[str, Any]       # kind-specific, see below

@dataclass
class Callsite:
    file: str                  # path relative to project root when possible
    line: int
    function: str
    code: str | None           # the source line, stripped
    stack: list[Frame]         # up to 12 project frames, innermost last

@dataclass
class Frame: file: str; line: int; function: str

@dataclass
class Trace:
    id: str
    framework: Literal["django", "fastapi", "other"]
    method: str
    path: str                  # concrete path, e.g. /orders/42/
    route: str | None          # template, e.g. /orders/<int:pk>/ or /orders/{id}
    view: str | None           # dotted name, e.g. shop.views.order_detail
    status: int
    started_at: float          # unix epoch seconds
    duration_ms: float
    query_count: int
    query_ms: float
    request: dict              # {"headers": {...}, "query": {...}, "body": str|None, "body_truncated": bool, "client": str|None}
    response: dict             # {"headers": {...}, "body": str|None, "body_truncated": bool, "size": int|None}
    spans: list[Span]
    issues: list[Issue]
    tags: dict[str, str]       # free-form; reserved keys: "run_id", "step" (load tester), "git_sha", "sampled_by"
    profile: dict | None       # speedscope JSON (only on profiled requests); stored separately, see storage
    error: dict | None         # {"type": str, "message": str, "traceback": str} for unhandled exceptions (never for a 4xx)
    truncated: dict | None     # {"dropped_spans": int, "max_spans": int} when max_spans_per_trace was hit
```

Errors: a request is an *error* (for `always_keep_errors`, `sampled_by: "error"` and `EndpointStats.error_rate`) when its status is 5xx, or when it raised and the response is not a 4xx (`redline_core.sampling.is_error`). Django's `Http404`/`PermissionDenied`/`BadRequest` are client errors: the trace has `error: None`.

Span `data` by kind:

| kind | data keys |
|---|---|
| sql | `sql` (raw, params substituted NOT done), `params` (list, redacted, JSON-safe reprs), `fingerprint` (str), `db_alias` (str), `vendor` ("postgresql"\|"mysql"\|"sqlite"\|"other"), `rows` (int\|None), `many` (bool), `explainable` (bool: SELECT/WITH…SELECT only) |
| cache | `op` (get/set/delete/get_many/...), `key`, `hit` (bool\|None), `backend` |
| http_out | `method`, `url` (query string redacted), `status` |
| template | `template` (name) |
| middleware / view / custom | anything JSON-safe |

## 2. Issues (`redline_core.detect`)

```python
@dataclass
class Issue:
    id: str                    # stable per (kind, fingerprint, callsite file:line) -> sha1 hex[:16], so the same problem dedupes across requests
    kind: Literal["n_plus_one", "duplicate_query", "slow_query", "too_many_queries"]
    severity: Literal["info", "warning", "critical"]
    title: str                 # "N+1: 24 queries on shop_orderitem from shop/views.py:41"
    detail: str                # one or two sentences of plain English
    span_ids: list[str]
    fingerprint: str | None
    callsite: Callsite | None
    count: int                 # number of offending queries
    total_ms: float
    suggestion: Suggestion | None

@dataclass
class Suggestion:
    summary: str               # "Add .prefetch_related('items') to the Order queryset"
    code: str | None           # the one-line fix, e.g. "orders = Order.objects.prefetch_related('items')"
    diff: str | None           # unified diff against callsite file when the suggester could locate the queryset; else None
    confidence: Literal["high", "medium", "low"]
    docs_url: str | None
```

Default thresholds (settings): N+1 at ≥ 5 repeats of the same fingerprint from the same callsite; duplicate = identical SQL+params ≥ 2; slow query ≥ 100 ms; too many queries ≥ 50 per request.

Detector details (implemented in `redline_core.detect`):

- N+1 only counts read queries (`explainable: true`). A duplicate group whose spans all belong to an N+1 issue is not reported again.
- `too_many_queries` has no fingerprint or callsite, so its id uses `"<METHOD> <route or path>"` as the location: one issue per endpoint.
- Severity: N+1 is `critical` at ≥ 4×threshold repeats or ≥ `slow_query_ms` in total, else `warning`. Duplicate is `warning` at ≥ 5 copies or ≥ `slow_query_ms` in total, else `info`. Slow query is `critical` at ≥ 5×`slow_query_ms`, else `warning`. Too many queries is `critical` at ≥ 2×limit, else `warning`.
- Suggesters run in registration order, and the first non-`None` result wins. A built-in `GenericSuggester` (confidence `low`) is always last. Exceptions from a suggester are logged and skipped.

Adapters plug a `FixSuggester` protocol into the detector:

```python
class FixSuggester(Protocol):
    def suggest(self, issue: Issue, spans: list[Span]) -> Suggestion | None: ...
```

## 3. Recorder (`redline_core.recorder`)

- `Recorder(settings, store=None, suggesters=())` ties together sampling, redaction, detection and storage. `configure(settings, store, suggesters) -> Recorder` installs the process default and `get_recorder()` returns it. The module-level `start_trace(recorder=None, **fields)` and `finish_trace(builder, **fields)` delegate to a recorder.
- `recorder.should_record(path) -> bool` returns False when Redline is disabled or the path is in `ignore_paths`. Adapters may call it to skip early.
- `start_trace(*, method, path, framework="other", route=None, view=None, headers=None, query=None, body=None, client=None, tags=None, profile_requested=False) -> TraceBuilder`.
  - `headers` is a mapping or ASGI `[(b"k", b"v")]`. `query` is a mapping or a raw query string. `body` is raw bytes or str.
  - Pass all of them **unredacted**. Redaction happens in `finish_trace`.
  - `profile_requested` is True when the adapter sees `?_redline=<flag>` or `X-Redline-Profile: <flag>`, where `<flag>` is one of `profile`, `1`, `true`, `yes`, `on` (case-insensitive). Both adapters call `redline_core.sampling.profile_requested(query, header)`, so they accept the same values. The core starts and stops pyinstrument itself.
  - The `X-Redline-Run: <id>;step=<n>` header becomes the tags `run_id` and `step`.
  - For an ignored path the builder is inert: nothing is recorded or stored.
- `builder.set_route(route, view=None)` and `builder.tag(key, value)` can be called once routing is known.
- `finish_trace(builder, *, status, headers=None, body=None, size=None, route=None, view=None, error=None) -> Trace`.
  - `headers` and `body` are the raw response values. `error` is the unhandled exception or a dict.
  - It always returns the trace. When sampling keeps it, `tags["sampled_by"]` is set (`all`/`rate`/`slow`/`error`/`profile`), detectors have run, and it was enqueued to the store.
- `current() -> TraceBuilder | None` (contextvar). Must be correct across `asyncio` tasks, `sync_to_async`, `copy_context()` threads.
- `record_span(kind, name, start, duration, data=None, capture_callsite=True, *, callsite=None) -> Span | None` – adapters' DB/cache hooks call this. It is a cheap no-op when no trace is active. **Units:** `start` is the `time.perf_counter()` reading at the start of the operation, and `duration` is in **seconds** (`perf_counter() - start`).
- `record_query(sql, params=None, *, start, duration, db_alias="default", vendor="other", rows=None, many=False, capture_callsite=True) -> Span | None` is the preferred SQL hook.
  - It fills `fingerprint` and `explainable`, which is always False for `many=True`.
  - It redacts params by column name, makes them JSON-safe, and keeps the first 10 rows for `executemany`.
  - Pass params in the driver's paramstyle as captured. EXPLAIN reuses them.
- `span(name, kind="custom", **data)` – public context manager AND decorator (sync + async) for user code. Exported as `redline_core.span`.
- Span cap: a trace stores at most `max_spans_per_trace` spans (`middleware` and `view` spans are always kept). Further spans are counted, not stored: `query_count`/`query_ms` stay exact, SQL spans are folded into per-(fingerprint, callsite) counters (`OverflowGroup`) that the N+1 and slow-query detectors add to the stored spans, and the trace gets `truncated`. Duplicate detection only sees stored spans.
- Writes go through `Recorder.store.save(trace)` which must not block the request (queue + background writer thread).

## 4. Storage (`redline_core.storage`)

`Store` protocol:

- `path`
- `save(trace)`, which never blocks
- `get(trace_id, *, include_profile=True) -> Trace | None`
- `get_profile(trace_id) -> dict | None`
- `has_profile(trace_id) -> bool`
- `list(filters: Filters | None, limit=50, cursor=None) -> Page`
- `endpoints(window_seconds) -> list[EndpointStats]`
- `issues(window_seconds, kind=None) -> list[IssueGroup]`
- `delete(trace_id) -> int`, `clear() -> int` and `prune(max_age_s=None, max_rows=None) -> int`, each returning the number of rows deleted. For `prune`, `None` means "use the store's rule" and `0` disables that rule for the call (it never deletes everything; that is `clear()`); negative values raise `ValueError`
- `count() -> int`
- `flush(timeout) -> bool`
- `close()`

Default `SQLiteStore(path=".redline/redline.db", *, background=True, queue_size=1000, max_rows=10_000, max_age_s=7 days, prune_every=200)` (`path` may be any `os.PathLike`; `max_rows`/`max_age_s` of `0` or `None` disable that rule):

- It uses its own file in WAL mode, never the app DB. Profiles are stored in a separate table.
- A daemon writer thread drains a bounded queue. When the queue is full, `save` drops the trace and increments `store.dropped`. The thread starts lazily on the first `save` in each process.
- Fork-safe: after `fork()` (gunicorn `--preload`, uWSGI without `lazy-apps`) the child gets a fresh connection, queue and writer (`os.register_at_fork(after_in_child=...)`, plus a PID check on every call). The inherited connection is never used or closed in the child.
- The writer prunes every `prune_every` writes.
- `SQLiteStore.from_settings(settings)` builds one from `Settings`. `":memory:"` works.

`endpoints` groups by `(route or path, method)`. `series` lists only **non-empty** buckets, at most 48, where `t` is the bucket start in epoch seconds. Issue groups are deduplicated by `Issue.id`, and the sample is the latest occurrence.

## 5. JSON HTTP API (served by adapters under their mount prefix; frontend uses relative URLs `./api/...`)

All responses JSON. Errors: `{"error": {"code": str, "message": str}}` with 4xx/5xx – including the adapters' own access and CSRF refusals (FastAPI renders any `HTTPException` raised by the Redline guards or the user's auth dependencies in this shape, keeping headers such as `WWW-Authenticate`).

Implementation: `redline_core.api.RedlineAPI(store, settings, framework=..., explain_executor=factory)`.

- `api.dispatch(method, path, params=None, body=None) -> (status, payload)`. `path` is relative to the mount, for example `"api/traces/abc"`, and leading or trailing slashes are ignored. `params` is a mapping of query parameters, where values may be str or lists. `body` is raw bytes/str or a parsed dict. Adapters just route everything under `api/` here.
- `factory(db_alias, vendor) -> executor | None`. The `executor(sql, params) -> rows` must run inside a transaction that it **always rolls back**, and must handle its own driver's paramstyle.
- Error codes (core API unless marked *adapter*):
  - 400: `invalid_filter`, `invalid_window`, `invalid_kind`, `invalid_json`, `invalid_body`, `not_explainable`, `unsupported_vendor` (EXPLAIN on a database that is not PostgreSQL/MySQL/SQLite), `analyze_unsupported` (MySQL)
  - 401: `unauthorized` (*adapter*, FastAPI: an auth dependency raised 401)
  - 403: `analyze_disabled`; `forbidden` (*adapter*: Django `PERMISSION` refused, FastAPI `local_only` refused the client address or the `Host` header, or an auth dependency raised 403); `csrf_failed` (*adapter*: Django's CSRF check, FastAPI's Origin / `Sec-Fetch-Site` check on DELETE/POST)
  - 404: `not_found` (also a missing UI asset), `trace_not_found`, `span_not_found`, `profile_not_found`
  - 405: `method_not_allowed`
  - 422: `explain_failed`
  - 500: `internal_error`
  - 501: `explain_unavailable`
  - FastAPI maps any other `HTTPException` status from a user dependency to `bad_request` (400), `not_found` (404), `too_many_requests` (429) or `http_error`.
  - Django rejects a `Host` not in `ALLOWED_HOSTS` on every Redline view with Django's own 400 (`DisallowedHost`), before any JSON is produced.
- `status` also accepts `1xx` and `3xx`. `window` also accepts `<n>m|h|d` and defaults to `24h`. `limit` is clamped to 1..500.

| Method & path | Returns |
|---|---|
| `GET api/meta` | `{"version", "framework", "settings": {"thresholds": {"n_plus_one", "duplicate", "slow_query_ms", "too_many_queries"}, "sampling": {"sample_rate", "always_keep_slower_than_ms", "always_keep_errors"}, "profiling_enabled", "profile_mode", "profiler_available", "explain_analyze_enabled"}, "storage": {"rows", "path", "dropped"}}` |
| `GET api/traces?limit=50&cursor=&q=&method=&status=2xx\|4xx\|5xx\|<int>&min_ms=&min_queries=&has_issues=1&route=&tag=k:v` | `{"items": [TraceSummary], "next_cursor": str\|null, "total": int}` |
| `GET api/traces/{id}` | full `Trace` (without `profile`; with `has_profile: bool`) |
| `GET api/traces/{id}/profile` | speedscope JSON or 404 |
| `DELETE api/traces/{id}` / `DELETE api/traces` | `{"deleted": int}` |
| `GET api/endpoints?window=1h\|24h\|7d` | `{"items": [EndpointStats]}` |
| `GET api/issues?window=...&kind=` | `{"items": [IssueGroup]}` |
| `POST api/explain` body `{"trace_id", "span_id", "analyze": false}` | `ExplainResult` |

```text
TraceSummary  = {id, method, path, route, view, status, started_at, duration_ms, query_count, query_ms, issue_count, issue_kinds: [str], has_profile, tags}
EndpointStats = {route, method, count, error_rate, p50_ms, p95_ms, p99_ms, avg_queries, max_queries, avg_query_ms, issue_count, series: [{t, p95_ms, count}]  (<=48 buckets)}
IssueGroup    = {id, kind, severity, title, detail, occurrences, first_seen, last_seen, routes: [str], sample_trace_id, sample_span_ids, suggestion, callsite}
ExplainResult = {vendor, analyzed: bool, plan: Any (raw: JSON for pg/mysql, rows for sqlite), text: str, tree: PlanNode, findings: [Finding], suggested_indexes: [str]}
PlanNode      = {label, detail, cost: float|None, rows: float|None, actual_ms: float|None, children: [PlanNode], flags: [str]}
Finding       = {severity, title, detail}
```

EXPLAIN safety:

- Only spans with `explainable: true` can be explained, and `run_explain` re-checks the SQL. Explainable means a single `SELECT`/`WITH`/`VALUES`/`TABLE` statement with no data-modifying CTE (`WITH x AS (DELETE …)`), no `SELECT … INTO`, no row-locking clause (`FOR UPDATE`, `FOR NO KEY UPDATE`, `FOR SHARE`, `FOR KEY SHARE`, `LOCK IN SHARE MODE`) and no second statement. Keywords match case-insensitively, and the text is checked twice: once literal-aware, once with a pessimistic reading of quoting (backslashes in strings, dollar quotes, MySQL `/*! */` and `--` comments). A statement that is ambiguous between dialects is refused.
- Functions with side effects (`nextval`, `pg_advisory_lock`, user functions that write) are not detected. A plain EXPLAIN never executes them; `EXPLAIN ANALYZE` (off by default) does, inside the rolled-back (and on PostgreSQL `READ ONLY`) transaction.
- EXPLAIN runs in a transaction that is always rolled back.
- `analyze` is refused (403 `analyze_disabled`) unless settings allow it.

PlanNode `flags` are `seq_scan`, `full_scan`, `sort_spill`, `hash_spill`, `nested_loop_high_rows`, `temp_sort` and `automatic_index`.

## 6. Settings (`redline_core.conf.Settings`, adapters map `REDLINE = {...}` / `install(settings=...)`)

| name | type | default |
|---|---|---|
| enabled | bool | True |
| storage_path | str (any `os.PathLike` is converted) | ".redline/redline.db" |
| sample_rate | float 0–1 | 1.0 |
| always_keep_slower_than_ms | float | 500 |
| always_keep_errors | bool | True |
| ignore_paths | list[str] (prefix or regex) | static/media/favicon + the mount prefix itself |
| capture_request_body / capture_response_body | bool | True / True |
| max_body_bytes | int | 20_000 |
| redact_headers | list[str] | authorization, cookie, set-cookie, x-api-key, proxy-authorization |
| redact_keys | list[str] regex | (?i)pass(word)?\|secret\|token\|api[_-]?key\|(?<![a-z])auth(orization)?(?![a-z])\|session\|csrf\|card\|cvv\|ssn\|private[_-]?key (`auth`/`authorization` only as a whole word, so `authenticated`/`author`/`author_id`/`authority`/`oauth_provider` stay visible) |
| n_plus_one_threshold | int | 5 |
| duplicate_threshold | int | 2 |
| slow_query_ms | float | 100 |
| too_many_queries | int | 50 |
| profile | "off" \| "opt_in" \| "sampled" \| "all" | "opt_in" (`?_redline=profile` or header `X-Redline-Profile: 1`) |
| profile_sample_rate | float | 0.0 |
| explain_analyze | bool | False |
| max_rows / max_age_days | int \| None | 10_000 / 7 (`0` or `None` disables that retention rule; negative is an error) |
| max_spans_per_trace | int \| None | 2_000 (`0` or `None`: no cap; see §3) |
| project_root | str\|None (any `os.PathLike` is converted) | auto (cwd) – for callsite filtering |
| writer_queue_size | int | 1_000 (background writer queue bound; overflow dropped + counted) |

Adapter-only settings: Django `REDLINE["PERMISSION"]` (callable or dotted path) and `REDLINE["STORE"]` (a `Store` instance, a callable/class taking the core `Settings` and returning one, or the dotted path of either; mirrors FastAPI's `install(store=...)`). FastAPI's `install(dependencies=None)` uses `local_only`: loopback client **and** a loopback `Host` (`localhost`, `*.localhost`, `127.0.0.0/8`, `[::1]`, any port), against DNS rebinding; `loopback_guard(allowed_hosts=[...])` builds a variant. With `enabled=False` FastAPI adds no middleware and no SQLAlchemy listeners.

`Settings.from_mapping(dict)` matches keys case-insensitively (`SAMPLE_RATE` works). Unknown keys raise `SettingsError` with a "did you mean" hint. `ignore_paths` entries that start with `^` are regular expressions; all other entries are path prefixes. Adapters append their own mount prefix.

## 7. UI

React 19 + Vite + TS + Tailwind, source in `frontend/`, built with `base: './'` into `packages/redline-core/src/redline_core/ui/static/` (index.html + assets/). Adapters serve `index.html` for the mount root and SPA routes, and the `assets/` files. Hash routing (`#/requests/:id`) so adapters need no catch-all routes.

Python helpers live in `redline_core.ui`, the package `ui/__init__.py` next to `static/`:

- `static_dir()`
- `index_html()`, which returns a placeholder page if the bundle is missing
- `asset_path(relative) -> Path | None`, which refuses traversal outside `static/`
- `content_type(path)`
- `is_built()`

---

# v0.2 additions

Everything below extends §1–§7; nothing above changes meaning. Stored rows written by v0.1 must
still load: every new field has a default and `from_dict` tolerates its absence.

## 8. Model additions (`redline_core.model`)

```python
SpanKind = Literal["sql", "cache", "http_out", "template", "middleware", "view", "custom",
                   "serializer", "resolver", "transaction", "blocking", "pool_wait"]
IssueKind = Literal["n_plus_one", "duplicate_query", "slow_query", "too_many_queries",
                    "blocking_in_async", "large_result", "unbounded_query", "inefficient_count",
                    "slow_serializer", "long_transaction", "external_in_transaction",
                    "slow_external", "duplicate_external", "retry_storm"]

@dataclass
class LogRecord:
    t_ms: float                # ms since trace start
    level: str                 # "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"
    logger: str
    message: str               # formatted, redacted, capped at 2_000 chars
    callsite: Callsite | None  # pathname:lineno of the log call (project-relative when possible)

Trace.logs: list[LogRecord] = []            # capped at Settings.max_logs_per_trace (extra counted in Trace.logs_dropped: int = 0)
Trace.release: str | None = None            # Settings.release resolved at configure time
Issue.extra: dict[str, Any] = {}            # kind-specific facts (rows, resolver path, host, ...)
```

`Trace.error` (when `capture_locals` is on) gains `"frames": [{"file", "line", "function", "code", "locals": {name: repr}}]`:
project frames only (innermost last, at most 20), at most 30 locals per frame, each `repr` capped at 300 chars,
names matching `redact_keys` replaced by `"[redacted]"`.

Span `data` for the new kinds and new keys:

| kind | data keys |
|---|---|
| sql (new keys) | `rows` filled where the driver/ORM exposes it, `in_transaction` (bool), `wide_columns` (list[str]: text/blob/json columns selected, Django/SQLAlchemy only when known), `has_limit` (bool) |
| serializer | `library` ("drf" \| "pydantic" \| "marshmallow" \| "other"), `serializer` (class name), `items` (int\|None), `many` (bool) |
| resolver | `path` ("orders.0.customer" with list indexes as `*`: "orders.*.customer"), `field`, `parent_type` |
| transaction | `db_alias`, `outcome` ("commit" \| "rollback"), `queries` (int), `http_out` (int: outbound calls made inside) |
| blocking | `reason` ("loop_lag" \| "sync_call"), `call` (e.g. "time.sleep", "requests.get", "sync ORM"; None when unknown), `lag_ms`; `sync_call` also has `span_id` (the SQL span) |
| pool_wait | `db_alias`, `pool_size`, `checked_out` |

## 9. Detectors (`redline_core.detect`, new modules in `redline_core.detectors`)

`redline_core.detectors/<name>.py` each exposes `def detect(trace: Trace, settings: Settings) -> list[Issue]`.
`redline_core.detectors.REGISTRY: list[Callable]` lists them; `Detector.detect` runs the v0.1 built-ins, then every
registry entry, then suggesters on every issue. One exception in one detector is logged and skipped.

| kind | fires when | default threshold setting |
|---|---|---|
| blocking_in_async | a `blocking` span ≥ `blocking_ms` in an async request | `blocking_ms = 20` |
| large_result | a SELECT returned ≥ `large_result_rows` rows | `1000` |
| unbounded_query | a SELECT with `has_limit: false` returned ≥ `unbounded_rows` rows | `200` |
| inefficient_count | callsite `code` matches `len(<qs>)` / `if <qs>:` / `bool(<qs>)` / `.count() > 0` patterns on a full-row SELECT (suggest `.count()` / `.exists()`) | – |
| slow_serializer | a `serializer` span ≥ `slow_serializer_ms` | `50` |
| long_transaction | a `transaction` span ≥ `long_transaction_ms` | `200` |
| external_in_transaction | a `transaction` span with `http_out > 0` | – |
| slow_external | an `http_out` span ≥ `slow_external_ms` | `500` |
| duplicate_external | same method+URL ≥ 2 times | – |
| retry_storm | same method+host ≥ 3 times with ≥ 1 failed (5xx, 429 or no status) | – |

N+1 over GraphQL: when the queries' common parent is a `resolver` span, the issue's `extra["resolver_path"]`
is set and the title reads `N+1: 24 queries on shop_customer from resolver orders.*.customer`.

## 10. Settings additions (`Settings`, same validation rules as §6)

| name | type | default |
|---|---|---|
| blocking_ms | float | 20 |
| detect_blocking | bool | True (async adapters run an event-loop lag monitor only while a request is in flight) |
| large_result_rows / unbounded_rows | int | 1_000 / 200 |
| slow_serializer_ms / long_transaction_ms / slow_external_ms | float | 50 / 200 / 500 |
| capture_logs | bool | True |
| log_level | str | "INFO" (minimum level captured into a trace) |
| max_logs_per_trace | int | 200 |
| capture_locals | bool | True (redacted by `redact_keys`) |
| editor | str | "vscode" – one of vscode, cursor, pycharm, idea, sublime, none, or a template containing `{file}` and `{line}` (`{file}` is absolute) |
| server_timing | bool | True – add a `Server-Timing` header to recorded responses |
| badge | "auto" \| bool | "auto" – inject the floating badge into HTML responses; `auto` = on when the framework is in debug mode |
| release | str \| None | None → auto: env `REDLINE_RELEASE`, else `GIT_SHA`/`SOURCE_VERSION`/`RENDER_GIT_COMMIT`/`HEROKU_SLUG_COMMIT`, else `git rev-parse --short HEAD` in `project_root` (once, 1 s timeout), else None |
| history_retention_days | int \| None | 90 (rollups/issue index/runs; independent of `max_age_days`) |
| load_from_ui | bool | False – allow `POST api/runs` |

## 11. Storage additions

`redline_core.histogram.Histogram`: log-linear (HDR-like, ~1% relative error, 1 µs … 1 h), mergeable,
`record(ms)`, `merge(other)`, `percentile(p)`, `count`, `max`, `mean`, `to_bytes()` / `from_bytes()`. Zero dependencies.

SQLite schema gets a `meta(key, value)` table with `schema_version`; migrations are additive and run on connect.
New tables (written by the writer thread in the same transaction as the trace):

- `trace_sql(trace_id, started_at, route, method, fingerprint, sql, count, total_ms, max_ms, rows)` – one row per (trace, fingerprint); pruned with traces.
- `trace_ext(trace_id, started_at, method, host, url_pattern, count, total_ms, max_ms, errors)` – outbound calls.
- `trace_cache(trace_id, started_at, backend, key_pattern, gets, hits, misses, sets, deletes)` – `key_pattern` replaces digit runs, uuids and ≥8-char hex with `*`.
- `rollups(bucket, release, route, method, count, errors, sum_ms, hist, sum_queries, sum_query_ms, issues)` – hourly, upserted per trace, kept `history_retention_days`.
- `releases(release, first_seen, last_seen)`.
- `issue_index(issue_id, kind, severity, title, route, first_seen, last_seen, first_release, last_release, occurrences)` – survives trace pruning.
- `runs(id, created_at, finished_at, status, name, config, summary)` and `run_steps(run_id, step, started_at, ended_at, target_rps, achieved_rps, sent, ok, errors, dropped, hist, error_breakdown)`.

New `Store` methods (SQLiteStore implements all; a custom store may raise `NotImplementedError`, which the API maps to 501 `not_supported`):
`search_sql(q, window, limit)`, `sql_workload(window)`, `cache_stats(window)`, `external_stats(window)`,
`history(route, method, start, end, bucket)`, `releases()`, `compare_periods(a, b)`, `issue_index(window=None)`,
`save_run(run)`, `save_run_step(run_id, step)`, `finish_run(run_id, status, summary)`, `get_run(id)`, `list_runs(limit)`, `delete_run(id)`, `run_bottlenecks(run_id)`,
`traces_since(started_at, trace_id, limit)` (for live tail).

## 12. API additions (§5 rules apply)

| Method & path | Returns |
|---|---|
| `GET api/meta` | adds `project_root`, `editor`, `editor_url_template` (str\|null, with `{file}`/`{line}`), `release`, `features: {load_from_ui, sse, snapshot}` |
| `GET api/traces` | adds filters `sql=<substring>` and `since=<epoch>`, `replay_of=<id>` |
| `GET api/stream?since=<epoch>` | **SSE** (adapter-served): `event: trace` with a TraceSummary per new trace, `: ping` every 15 s, closes after 5 min. Core helper `api.poll(since, last_id) -> list[TraceSummary]` |
| `GET api/traces/{id}/snapshot` | `text/html` standalone page (bundle inlined, `window.__REDLINE_SNAPSHOT__ = {trace, profile}`). `dispatch` returns a `RawResponse(status, content_type, body: bytes, headers)` for it; adapters send it as-is |
| `GET api/compare?a=<id>&b=<id>` | `{a: TraceSummary, b: TraceSummary, delta: {duration_ms, query_count, query_ms}, by_kind: {kind: {a_ms, b_ms, a_count, b_count}}, sql: [{fingerprint, sql, a_count, b_count, a_ms, b_ms}], issues: {only_a, only_b, both}}` |
| `GET api/sql?q=&window=` | `{items: [{fingerprint, sql, count, traces, total_ms, max_ms, routes, sample_trace_id}]}` |
| `GET api/indexes?window=` | `{candidates: [{table, columns, create_sql, covers: [fingerprint], queries, total_ms, est_saving_ms, vendor}], unused: [{table, index, scans, size_bytes}] , unused_supported: bool}` |
| `GET api/cache?window=` | `{items: [{backend, key_pattern, gets, hits, misses, hit_rate, sets, unread_sets, stampedes}]}` |
| `GET api/external?window=` | `{items: [{method, host, url_pattern, count, p95_ms, max_ms, errors, error_rate, duplicates, retries}]}` |
| `GET api/history?route=&method=&window=30d&bucket=1h\|1d` | `{series: [{t, count, p50_ms, p95_ms, p99_ms, error_rate, avg_queries}], releases: [{release, first_seen}]}` (no route = whole app) |
| `GET api/releases` | `{items: [{release, first_seen, last_seen, requests}]}` |
| `GET api/history/compare?a=&b=` | `a`/`b` are `release:<name>` or `range:<start>-<end>` (epoch seconds). `{a, b, endpoints: [{route, method, a: Stats, b: Stats, delta_p95_pct, delta_queries, verdict: "regressed"\|"improved"\|"same"\|"new"\|"gone"}], issues: {new: [...], resolved: [...], persisting: [...]}}`; regressed = p95 +20% and +5 ms, or avg queries +1 |
| `GET api/runs` / `GET api/runs/{id}` | runs list / `{id, name, status, config, created_at, finished_at, steps: [Step], summary: {knee_step, max_ok_rps, verdict}}` with `Step = {step, target_rps, achieved_rps, sent, ok, errors, dropped, p50_ms, p95_ms, p99_ms, max_ms, error_rate, error_breakdown}` |
| `GET api/runs/{id}/bottlenecks` | `{steps: [{step, target_rps}], items: [{key, kind, label, callsite, per_step: [{step, mean_ms, p95_ms, count}], growth: float}]}` sorted by growth – the "query X went 3 ms → 410 ms" view |
| `POST api/runs` / `POST api/runs/{id}/stop` / `DELETE api/runs/{id}` | start (403 `load_disabled` unless `load_from_ui`; the target is always the app's own base URL, given by the adapter) / stop / delete |

New error codes: 403 `load_disabled`, 404 `run_not_found`, 409 `run_active`, 501 `not_supported`.

Replay is browser-side: the UI re-sends the request with `fetch` (same origin, the viewer's own cookies),
adding `X-Redline-Replay-Of: <trace id>`; the recorder turns that header into the tag `replay_of`
(like `X-Redline-Run`). Redacted values must be re-entered by the user; they are never sent as `[redacted]`.

## 13. Load tester (`redline_core.load`, extra `redline-core[load]` = httpx; console script `redline`)

- `redline load SCENARIO [--rps N | --ramp A:B | --steps A:B:STEP] [--duration 60s] [--step-duration 30s] [--workers N] [--store PATH] [--json OUT] [--fail-p95-ms X] [--fail-error-rate Y]`, `redline load init --from-openapi URL|FILE | --from-traces [--store PATH]`. Exit code 1 when a `--fail-*` threshold is breached (CI-ready).
- Scenario: JSON, TOML (3.11+) or a Python file defining `SCENARIO = {...}`:
  `{name, base_url, headers, timeout_s, requests: [{name, method, path, weight, headers, json|body, expect_status}], profile: {mode: "constant"|"ramp"|"steps", rps, start_rps, end_rps, step_rps, duration_s, step_duration_s, stop_on: {error_rate, p95_ms}}, workers}`.
- **Open model**: arrivals are scheduled at the target rate independent of responses (fixed interval or `poisson: true`); latency is measured from the *scheduled* time (coordinated-omission corrected), with a per-worker in-flight cap (beyond it: counted `dropped`). Workers are separate **processes** (asyncio + httpx each) sending per-second histograms to the parent.
- Every request carries `X-Redline-Run: <run_id>;step=<n>` so app-side traces are tagged; the run (steps, histograms) is written to the same store the app uses (`--store`, default `.redline/redline.db`) and appears in the UI.
- Knee: the first step whose p95 > 2× the first step's, or error rate > 1 %, or achieved < 90 % of target. `summary.knee_step`, `summary.max_ok_rps`.
- Scenario generators: OpenAPI (FastAPI/Litestar/Ninja), captured traffic (store; top endpoints, concrete paths, no bodies with redacted values), Django URLconf (`manage.py redline_scenario`), Flask `url_map` (`flask redline scenario`).

## 14. Integrations (`redline_core.contrib.*`, lazy: never imported by `redline_core/__init__`)

- `contrib.sqlalchemy`: `instrument_engine(engine)` (moved from fastapi-redline, which re-exports it), `SQLAlchemyFixSuggester`, `explain_executor_factory`, plus `transaction` spans (begin/commit/rollback events) and `pool_wait` spans (checkout time ≥ 1 ms).
- `contrib.httpx` (moved), `contrib.requests`, `contrib.redis` (redis-py sync + asyncio → `cache` spans with `op` = command, `key`, `hit` for GET-like commands), `contrib.asyncpg`, `contrib.psycopg` (psycopg 3 without an ORM → `sql` spans).
- `contrib.strawberry` (`RedlineExtension`) and `contrib.graphene` (`RedlineMiddleware`) → `resolver` spans.
- `contrib.asgi`: async helpers shared by ASGI adapters (event-loop lag monitor, Server-Timing/badge body injection).
  The lag monitor itself is zero-dependency core: `redline_core.blocking.track_request(builder)` (sync + async context manager, re-exported by `contrib.asgi`) watches the running loop while the request is in flight (no-op without a running loop, for an inert builder or with `detect_blocking` off); stalls still in progress are flushed through `TraceBuilder.on_finish` callbacks, which `finish_trace` runs first. SQL hooks of *sync* drivers call `redline_core.blocking.note_sync_call(span, call)` after recording. `redline_core.transactions.begin_transaction(builder, callsite)` / `end_transaction(state, db_alias=, outcome=)` build `transaction` spans (empty transactions and control statements such as `SAVEPOINT` are not counted/recorded).
- Core helpers used by every adapter: `TraceBuilder.server_timing() -> str` (`total;dur=…, db;dur=…;desc="12 queries", cache;dur=…, http;dur=…, tpl;dur=…, redline;desc="<trace id>"`) and `redline_core.ui.badge_snippet(trace_id, mount_url, summary) -> str` (a `<script src=".../assets/badge.js" data-…>` tag; badge.js is a dependency-free script shipped in the bundle).

## 15. Adapters

| package | python | framework | entry point |
|---|---|---|---|
| flask-redline (`flask_redline`) | ≥ 3.10 | Flask ≥ 3.0, < 4 | `Redline(app=None, *, engine=None, settings=None, url_prefix="/_redline", permission=None, store=None)` + `init_app(app)`; Flask-SQLAlchemy engines auto-detected; `flask redline clear\|prune\|scan\|scenario` |
| litestar-redline (`litestar_redline`) | ≥ 3.10 | Litestar ≥ 2.12, < 3 | `RedlinePlugin(settings=None, engine=None, path="/_redline", guards=None, store=None)` |

Flask and Litestar default to the same `local_only` policy as FastAPI (loopback client **and** loopback Host) and the same Origin / `Sec-Fetch-Site` CSRF check; a `permission` / `guards` hook replaces it. Django Ninja is served by django-redline (route templates resolved from Ninja's operations).
Every adapter: Server-Timing, badge injection (HTML, non-streaming, 200–299, `badge` on), SSE `api/stream`, `RawResponse` passthrough, `X-Frame-Options: DENY` on Redline's own responses.

## 16. Static N+1 scan (`redline_core.scan`)

`scan_templates(paths, dialect="django"|"jinja") -> list[ScanFinding]` finds `{% for x in y %}` blocks that read `x.a.b` / `x.a.all`;
`ScanFinding = {file, line, loop_var, chain, kind: "template"|"serializer", severity, message, suggestion}`.
Django (`manage.py redline_scan`) keeps only chains whose first hop is a real FK/O2O/M2M/reverse relation, and also checks DRF
`ModelSerializer`s with nested relation serializers whose view queryset lacks the matching `select_related`/`prefetch_related`.
Flask (`flask redline scan`) runs the Jinja dialect. Output: text table or `--json`; exit 1 with `--fail`.

## 17. UI additions

Command palette (⌘K / Ctrl-K), Live mode (SSE) on Requests, open-in-editor links on every callsite, copy as cURL / HTTPie,
Replay (modal for redacted values, optional profile), compare two traces (`#/compare/:a/:b`), Logs and Exception (with locals) tabs,
snapshot download, new pages: **Database** (SQL search, index advisor, unused indexes), **Cache**, **External**, **History**
(timeline with release markers, compare releases/periods, new/resolved issues) and **Load** (runs, RPS-vs-p95-vs-errors chart with knee, per-step bottlenecks, start form when `load_from_ui`).
