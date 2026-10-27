# Redline internal contract (v0.1 + v0.2)

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
    framework: Literal["django", "fastapi", "flask", "litestar", "other"]
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
| http_out | `method`, `url` (query string redacted; a userinfo password becomes `%5Bredacted%5D`, see §6), `status` |
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
- `delete(trace_id) -> int`, `clear() -> int` (SQLiteStore: `clear(history=False)`, see §11) and `prune(max_age_s=None, max_rows=None) -> int`, each returning the number of rows deleted. For `prune`, `None` means "use the store's rule" and `0` disables that rule for the call (it never deletes everything; that is `clear()`); negative values raise `ValueError`
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
  - 401: `unauthorized` (*adapter*, FastAPI: an auth dependency raised 401); `login_required` (access gate, §18: `token`/`basic` policy and no valid credentials; the error object also has `"login": "token"|"basic"`); `invalid_credentials` (a wrong Bearer token, Basic password or `POST api/login`)
  - 403: `analyze_disabled`; `forbidden` (access gate: network rules, `framework`/`custom` refusal, `allowed_ips`; or an auth dependency raised 403); `csrf_failed` (access gate's Origin / `Sec-Fetch-Site` check on every state-changing method; Django's own CSRF check too); `read_only` (§18); `open_refused` (`access="open"` without `REDLINE_ALLOW_OPEN=1`)
  - 404: `not_found` (also a missing UI asset, every URL when `ui_enabled` is off, `POST api/login` without a login policy), `trace_not_found`, `span_not_found`, `profile_not_found`, `feature_disabled` (§18)
  - 405: `method_not_allowed`
  - 422: `explain_failed`
  - 429: `too_many_requests` (login rate limit; `Retry-After` header)
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

**Redaction rules** (`redline_core.redact.Redactor`, mask `"[redacted]"`):

- `url(u)`: secret query keys masked; `scheme://user:password@host` → `user:%5Bredacted%5D@host` (the mask is
  percent-encoded so the URL still parses); a URL with nothing to mask is returned unchanged.
- `text(t)` (log messages, exception locals): URL-looking tokens (`scheme://…`) go through `url()` first; then every
  `key<sep>value` pair (`=` or `:`, key optionally quoted) whose key matches `redact_keys` has its value masked. A
  quoted value is masked inside its quotes; an unquoted one (optionally `Bearer|Basic|Token|Digest ` + token) is
  masked up to the next whitespace, `,` `;` `&` `)` `}` `]` or quote. A non-secret key's unquoted value stops at
  `= ? & :` as well and the scan resumes *at* that value, so `url=https://h/?api_key=x`, `Cookie: sessionid=x`,
  `redirect: /cb?token=x` and `user=bob:password=x` all mask `x`. The whole-word `auth` rule applies
  (`author=bob`, `authenticated=true` stay visible).

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
project frames only (innermost last, at most 20), at most 30 locals per frame, each `repr` capped at 300 chars.
Redaction of locals: a local whose *name* matches `redact_keys` is `"[redacted]"` whatever its value; otherwise
dicts/lists/tuples are walked (depth 4, 50 items per container, the rest shown as `…`) and values under secret
keys masked; `str`/`bytes` values that parse as JSON are masked like a JSON body, all other text goes through
`Redactor.text`; finally the whole `repr` goes through `Redactor.text` (e.g. an `HttpRequest` repr with
`?token=…`).

Span `data` for the new kinds and new keys:

| kind | data keys |
|---|---|
| sql (new keys) | `rows` filled where the driver/ORM exposes it, `in_transaction` (bool), `wide_columns` (list[str]: text/blob/json columns selected, Django/SQLAlchemy only when known), `has_limit` (bool) |
| serializer | `library` ("drf" \| "pydantic" \| "marshmallow" \| "other"), `serializer` (class name), `items` (int\|None), `many` (bool) |
| resolver | `path` ("orders.0.customer" with list indexes as `*`: "orders.*.customer"), `field`, `parent_type` |
| transaction | `db_alias`, `outcome` ("commit" \| "rollback"), `queries` (int), `http_out` (int: outbound calls made inside) |
| blocking | `reason` ("loop_lag" \| "sync_call"), `call` (e.g. "time.sleep", "requests.get", "sync ORM"; None when unknown), `lag_ms`; `sync_call` also has `span_id` (the SQL span) |
| pool_wait | `db_alias`, `pool_size`, `checked_out` |

Notes (sql data): `has_limit` is computed in core from the SQL text (top-level `LIMIT`, `FETCH FIRST|NEXT`,
`TOP`, `ROWNUM`; a subquery's limit does not count). `rows` for SELECTs is `cursor.rowcount` when the driver has it,
else the number of rows the ORM/driver has fetched so far (adapters update `span.data["rows"]` as rows are fetched).
`wide_columns` is not on the hot path: the adapter's suggester fills `Issue.extra["wide_columns"]` (Django: field
names; SQLAlchemy: attribute names). `inefficient_count` issues carry `extra` = `pattern` (`len|bool|truthy|count_cmp`),
`expr`, `old`, `new`, `fix_line`.

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
| log_level | str | "INFO" (minimum level captured into a trace; capture follows the app's logger levels – see below) |
| max_logs_per_trace | int | 200 |
| capture_locals | bool | True (redacted by `redact_keys`) |
| editor | str | "vscode" – one of vscode, cursor, pycharm, idea, sublime, none, or a template containing `{file}` and `{line}` (`{file}` is absolute) |
| server_timing | "auto" \| bool | "auto" – add a `Server-Timing` header to recorded responses; `auto` = only in the framework's debug mode (it tells any client the trace id and query count/time, a side channel); the `production` preset sets False. Helper `redline_core.ui.server_timing_enabled(settings, debug)` |
| badge | "auto" \| bool | "auto" – inject the floating badge into HTML responses; `auto` = on when the framework is in debug mode |
| release | str \| None | None → auto: env `REDLINE_RELEASE`, else `GIT_SHA`/`SOURCE_VERSION`/`RENDER_GIT_COMMIT`/`HEROKU_SLUG_COMMIT`, else `git rev-parse --short HEAD` in `project_root` (once, 1 s timeout), else None |
| history_retention_days | int \| None | 90 (rollups/issue index/runs; independent of `max_age_days`) |
| load_from_ui | bool | False – allow `POST api/runs` |

Log level rule: the capture handler sits on the root logger, so it only sees records the app's loggers let
through. When the app configured logging (the root logger has a handler of its own when Redline installs) levels
are never touched. When logging is unconfigured (no root handler) and the root level (Python's default WARNING) is
above `log_level`, the root level is lowered to `log_level` (restored when the handler is uninstalled); console
output stays exactly as before: records below WARNING have no handler that prints them, and WARNING and above
still reach `logging.lastResort` (stderr) because the capture handler stands in for it when no other handler is on
the record's path.

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

Concrete decisions (v0.2 implementation):

- **Protocols.** `Store` is unchanged (the v0.1 surface; Django's `isinstance(store, Store)` check keeps accepting
  v0.1 custom stores). The new methods live on `redline_core.storage.ExtendedStore(Store, Protocol)`; a store
  lacking one (or raising `NotImplementedError`) makes the matching endpoint answer 501 `not_supported`.
  `window` arguments are seconds back from now. Value types (all with `to_dict()`, in `storage.base`):
  `SqlStats`, `CacheStats`, `ExternalStats`, `History`, `ReleaseInfo`, `PeriodStats`, `PeriodComparison`,
  `IssueIndexEntry`, `Bottlenecks`; `Period = ("release", name) | ("range", start, end)` and
  `parse_period("release:<name>" | "range:<start>-<end>")` (raises `FilterError`).
- **Schema.** `meta.schema_version` = 3. A v0.1/v0.2-early file is migrated in place on connect inside one
  `BEGIN IMMEDIATE` (re-checked inside, so concurrent workers migrate once): the new tables are created and
  **backfilled from the stored traces**. Version 3 adds `traces.saved_seq INTEGER` (indexed; backfilled from
  rowid order) and the `meta.last_seq` counter: the writer reserves `seq` numbers inside its write transaction,
  so they are unique and increasing across processes and never reused after pruning (a re-saved trace gets a
  new one). Every write transaction is `BEGIN IMMEDIATE` (several processes may write one file). Additive columns beyond the list above: `trace_sql.rows_n` (executions
  with a known row count), `trace_sql.vendor`, `trace_sql.db_alias`, `releases.requests`; one extra history
  table `issue_rollups(bucket, release, issue_id, occurrences)` (per-period issue sets for `compare_periods`).
- **trace_sql.** `fingerprint` is the 16-hex `sql.fingerprint_hash` (sha1 of the normalised statement); `sql` is
  the normalised statement (literals → `?`), which is also what `sql=` / `search_sql` match (case-insensitive
  substring). `rows` = sum of known row counts (NULL when none known). Spans folded by the span cap
  (`OverflowGroup`, not part of the stored trace) are not included. `route` = `route or path`.
- **Patterns.** `url_pattern` = `scheme://host[:port]` + path, no query; `key_pattern` = the key. Both replace UUIDs,
  hex runs of ≥ 8 chars and digit runs **not glued to a preceding letter or digit** with `*` (`/v2/users/42` →
  `/v2/users/*`, `user:42` → `user:*`). Cache ops: reads = `get, get_many, get_or_set, has_key, mget, hget, hmget,
  hgetall, exists, getex, getdel` (+ Django async `a…` variants), sets = `set, add, set_many, setex, psetex, setnx,
  mset, hset, hmset, incr, decr, incrby, decrby`, deletes = `delete, delete_many, del, unlink, hdel, clear`; spans
  without a key (redis pipelines) are skipped. An outbound call is an error when it has no status, a 5xx or a 429.
- **Write path.** The writer derives all rows from the batch outside its lock and writes them in the batch's
  transaction with `executemany`; rollups are read-modify-write (merged `Histogram` bytes). A re-saved trace id
  replaces its trace-scoped rows and is **not** counted again in rollups/releases/issue index. History reflects
  *kept* traces (sampling applies). `errors` in rollups uses `sampling.is_error`.
- **Retention.** Trace-scoped tables (`profiles`, `trace_tags`, `issues`, `trace_sql`, `trace_ext`, `trace_cache`)
  go with their trace (`delete`, `prune`, `clear`). `SQLiteStore(history_retention_s=90 days)` (`from_settings` maps
  `history_retention_days`; `0`/`None` = keep forever): `prune()` also calls `prune_history()`, which deletes
  rollup / issue-rollup hours that ended before the cutoff, releases and issue-index entries last seen before it, and
  non-running runs (with steps) created before it. `clear()` keeps the history; `clear(history=True)` wipes it too.
- **Reads.** `search_sql`/`sql_workload` → `SqlStats {fingerprint, sql, count (executions), traces, total_ms, max_ms,
  mean_ms, mean_rows, routes ["METHOD route"], sample_trace_id (latest), vendor}` sorted by total time.
  `cache_stats` → `hit_rate = hits/(hits+misses)` (null when unknown); `unread_sets` = the pattern's sets when it had
  no reads in the window; `stampedes` = bursts of ≥ 3 requests missing the same pattern within 1 s (request start
  times; each burst counted once). `external_stats` → `p95_ms` from a histogram where every call is taken at its
  request's mean for that pattern (exact when called once per request); `duplicates` = requests with ≥ 2 calls to the
  pattern, `retries` = requests with ≥ 3 calls and ≥ 1 error. `history` merges hourly rows into UTC-aligned 1 h / 1 d
  buckets (only non-empty ones); its `releases` are those first seen within the range. `compare_periods` has hourly
  resolution for ranges.
- **Runs** (`redline_core.runs`): `Run(id, name, status: "running"|"done"|"stopped"|"failed", config, created_at,
  finished_at, steps, summary)` and `RunStep(step, target_rps, started_at, ended_at, achieved_rps, sent, ok, errors,
  dropped, hist: Histogram, error_breakdown)`; `RunStep.error_rate = errors / (ok + errors)`. Run writes are
  synchronous (not queued); `save_run_step` upserts by `(run_id, step)`, so a step can be re-saved as it progresses.
  `delete_run` keeps the run's tagged traces. `run_bottlenecks` joins the `run_id`/`step` tags with `trace_sql`
  (kind `sql`, key `sql:<fingerprint>`, label = normalised SQL, callsite from the latest sample's span) and with
  `traces` (kind `route`, key/label `"METHOD route"`): per step `mean_ms` per execution (per request for routes),
  `p95_ms` over each request's mean, `count`; `growth = last mean / max(first mean, 0.01)` (1.0 with a single step);
  top 50 by growth. Steps come from `run_steps`, else from the tags (`target_rps: null`). `None` (→ 404) when there is
  neither the run nor a tagged trace.
- `traces_since(started_at, trace_id="", limit=100, after_seq=None)` → TraceSummaries (each with an extra `seq`)
  in **insertion order** (`saved_seq`), oldest saved first. Traces are written when they finish, so paging by
  `started_at` would never deliver a slow request that started before already-streamed fast ones. Without
  `after_seq`: traces with `started_at` strictly after `(started_at, trace_id)` (the initial position, an
  epoch); with it: traces saved after that `seq`.
- **Unmatched routes.** Rollups and the issue index key a request with no route as `"(unmatched)"`
  (`derive.UNMATCHED`), not by its path, so 404s and scanners do not add rows per path for 90 days. Trace rows,
  `trace_sql` and the per-trace `issues` table keep the real path.
- **Dead runs.** `get_run` / `list_runs` mark a `running` row older than 10 s whose `runs/<id>.pid` is missing or
  names a dead process as `failed` (`summary.message` = "the load process exited unexpectedly", persisted), so it
  can be deleted. Only when a `runs` directory sits beside the store file (runs without process control are left
  alone).

## 12. API additions (§5 rules apply)

| Method & path | Returns |
|---|---|
| `GET api/meta` | adds `project_root`, `editor`, `editor_url_template` (str\|null, with `{file}`/`{line}`), `release`, `features: {load_from_ui, sse, snapshot, explain, profile, replay, load, logs, locals}` (§18), `access: {policy, login, read_only, preset}` (§18) |
| `GET api/traces` | adds filters `sql=<substring>` and `since=<epoch>`, `replay_of=<id>` |
| `GET api/config` | effective configuration, sources and warnings (§18) |
| `GET api/stream?since=<epoch>[&after=<seq>]` | **SSE** (adapter-served): `event: trace` with a TraceSummary (plus `seq`) per newly saved trace, in save order; the SSE `id:` is the `seq`. `since` is only the initial position; `after=<seq>` or the `Last-Event-ID` header resumes after that `seq` (and wins over `since`). `: ping` every 15 s, closes after 5 min. Core helper `api.poll(since, last_id, limit=100, after_seq=None) -> list[TraceSummary]` |
| `GET api/traces/{id}/snapshot` | `text/html` standalone page (bundle inlined, `window.__REDLINE_SNAPSHOT__ = {trace, profile}`). `dispatch` returns a `RawResponse(status, content_type, body: bytes, headers)` for it; adapters send it as-is |
| `GET api/compare?a=<id>&b=<id>` | `{a: TraceSummary, b: TraceSummary, delta: {duration_ms, query_count, query_ms}, by_kind: {kind: {a_ms, b_ms, a_count, b_count}}, sql: [{fingerprint, sql, a_count, b_count, a_ms, b_ms}], issues: {only_a, only_b, both}}` |
| `GET api/sql?q=&window=` | `{items: [{fingerprint, sql, count, traces, total_ms, max_ms, routes, sample_trace_id}]}` |
| `GET api/indexes?window=&db=` | `{candidates: [{table, columns, create_sql, covers: [fingerprint], queries, total_ms, est_saving_ms, vendor, kind, confidence, verified}], unused: [{table, index, scans, size_bytes}], unused_supported: bool}` (full shape below) |
| `GET api/cache?window=` | `{items: [{backend, key_pattern, gets, hits, misses, hit_rate, sets, unread_sets, stampedes}]}` |
| `GET api/external?window=` | `{items: [{method, host, url_pattern, count, p95_ms, max_ms, errors, error_rate, duplicates, retries}]}` |
| `GET api/history?route=&method=&window=30d&bucket=1h\|1d` | `{series: [{t, count, p50_ms, p95_ms, p99_ms, error_rate, avg_queries}], releases: [{release, first_seen}]}` (no route = whole app) |
| `GET api/releases` | `{items: [{release, first_seen, last_seen, requests}]}` |
| `GET api/history/compare?a=&b=` | `a`/`b` are `release:<name>` or `range:<start>-<end>` (epoch seconds). `{a, b, endpoints: [{route, method, a: Stats, b: Stats, delta_p95_pct, delta_queries, verdict: "regressed"\|"improved"\|"same"\|"new"\|"gone"}], issues: {new: [...], resolved: [...], persisting: [...]}}`; regressed = p95 +20% and +5 ms, or avg queries +1 |
| `GET api/runs` / `GET api/runs/{id}` | runs list / `{id, name, status, config, created_at, finished_at, steps: [Step], summary: {knee_step, max_ok_rps, verdict}}` with `Step = {step, target_rps, achieved_rps, sent, ok, errors, dropped, p50_ms, p95_ms, p99_ms, max_ms, error_rate, error_breakdown}` |
| `GET api/runs/{id}/bottlenecks` | `{steps: [{step, target_rps}], items: [{key, kind, label, callsite, per_step: [{step, mean_ms, p95_ms, count}], growth: float}]}` sorted by growth – the "query X went 3 ms → 410 ms" view |
| `POST api/runs` / `POST api/runs/{id}/stop` / `DELETE api/runs/{id}` | start (403 `load_disabled` unless `load_from_ui`; the target is always the app's own base URL, given by the adapter) / stop / delete |

New error codes: 400 `invalid_period`, 403 `load_disabled`, 404 `run_not_found`, 409 `run_active`, 501 `not_supported`.

Concrete decisions (v0.2 implementation):

- `RedlineAPI(store, settings, *, framework, explain_executor, run_launcher=None, run_stopper=None)` and
  `dispatch(method, path, params=None, body=None, *, base_url=None)`; `base_url` is the app's own root URL, which
  adapters pass for `POST api/runs`.
- `run_launcher(config: dict, base_url: str | None) -> run_id` starts a run in the background (`ValueError` → 400
  `invalid_body`); `run_stopper(run_id) -> bool`. Without them `POST api/runs` / `POST api/runs/{id}/stop` answer
  501 `not_supported`. `POST api/runs` checks `load_from_ui` first (403), then validates the body (400
  `invalid_body`): `{name, requests: [{method, path, weight}], profile: {mode, rps | start_rps/end_rps[/step_rps],
  duration_s, step_duration_s}}`; every `path` must start with a single `/` (no scheme/host: the target is always the
  app). Answers 202 `{id, name, status: "running"}`. Stop answers `{stopped: bool}` (404 for an unknown run).
  `DELETE api/runs/{id}` → `{deleted: int}`, 409 `run_active` while running.
- `GET api/runs` → `{items: [Run]}` (same shape as the detail, steps included), `?limit=` (default 50).
- `RawResponse(status, content_type, body: bytes, headers: dict)` (`redline_core.api`) is returned **instead of a
  tuple** for the snapshot; it also unpacks as `status, payload = …` with `payload` being the RawResponse itself, so
  check `isinstance(payload, RawResponse)` (or the result). Snapshot: `text/html; charset=utf-8`,
  `Content-Disposition: attachment; filename="redline-<id>.html"`. `redline_core.api.snapshot_html(data)` builds it:
  CSS inlined as `<style>`, every JS chunk reachable from the entry (lazy ones included) rewritten to import
  `@redline/<chunk>` and mapped by an import map to `data:` URLs; `window.__REDLINE_SNAPSHOT__ = {trace (with
  has_profile), profile}` is set by a classic script **before** the import map and the entry module. JSON is escaped
  for `<script>` (`<`, `>`, `&`, U+2028/9).
- `api.poll(since, last_id=None, limit=100, after_seq=None) -> [TraceSummary]` in save order (`seq`) (falls back to `list(since=…)` when the
  store has no `traces_since`).
- `GET api/traces?since=` is exclusive and filters on `started_at` (unchanged); only the live tail follows save order.
- `GET api/traces`: `sql` matches the normalised SQL (case-insensitive substring), `since` is exclusive, invalid →
  400 `invalid_filter`.
- `GET api/compare`: `sql` rows keyed by the 16-hex fingerprint, `sql` = normalised statement, sorted by the
  largest time difference; `delta` and `*_ms` are `b − a`; issue lists hold `{id, kind, severity, title}`; missing
  `a`/`b` → 400 `invalid_filter`, unknown id → 404 `trace_not_found`.
- `GET api/sql?q=&window=24h&limit=100` → `SqlStats` items (extra keys `mean_ms`, `mean_rows`, `vendor`).
  `GET api/cache` items add `deletes`; `hit_rate` may be `null`. `GET api/external` items add `total_ms`.
- `GET api/history`: `window` default `30d`; `bucket` default `1h` for windows ≤ 2 d, else `1d` (other values → 400
  `invalid_filter`); `method` is case-insensitive.
- `GET api/history/compare`: `a` is the baseline, `b` the period under test. `a`/`b` echo the period:
  `{kind: "release", release, requests}` / `{kind: "range", start, end, requests}`. Endpoint `a`/`b` stats are
  `{count, error_rate, p50_ms, p95_ms, p99_ms, avg_ms, avg_queries}` or `null` (new/gone); `delta_p95_pct` is null
  when not computable. Verdicts: regressed = p95 ≥ +20 % **and** ≥ +5 ms, or avg queries ≥ +1; improved = the mirror
  image; sorted regressed, new, improved, gone, same. Issue items are `IssueIndexEntry` dicts (`id, kind, severity,
  title, route, first_seen, last_seen, first_release, last_release`) with `occurrences` counted within the period
  (`b`'s count for persisting).
- `GET api/indexes?window=24h&db=default&refresh=` (`redline_core.index_advisor.advise`) reads
  `store.sql_workload(window)` (no method → 501 `not_supported`). Answer:
  `{db, vendor, window, candidates: [Candidate], unused: [Unused], unused_supported: bool, unused_error:
  {code, message}|null, verified: bool, queries_analyzed, already_indexed, explained, elapsed_ms, cached: bool}`
  with `Candidate = {table, columns: [str], create_sql, covers: [fingerprint], queries (executions of the covered
  fingerprints), total_ms (their total time), est_saving_ms, vendor, kind: "btree"|"trigram"|"fts"|"fulltext",
  confidence: "high"|"medium"|"low", verified: bool, explained: bool, note: str|null}` (at most 10, best first) and
  `Unused = {schema, table, index, scans, size_bytes, drop_sql}`.
  - **Input.** The top 100 explainable `SELECT` fingerprints by total time whose sample trace's span ran on `db`
    (a missing sample counts as `default`). The sample span's raw SQL/params are parsed when available (so `LIKE`
    patterns are known), else the normalised SQL.
  - **Requirements** per table and per `OR`-branch cluster: equality columns (`=`, `IN`, `IS NULL`; any order),
    then one range column (`<`…, `BETWEEN`, known prefix `LIKE 'x%'`) or, without one, the single-table,
    single-direction `ORDER BY` columns (sort-only needs a `LIMIT`); ≤ 4 columns. `a.x = b.y` adds `(x)` on `a` and
    `(y)` on `b`. A bare `(id)` is skipped (primary key). No B-tree requirement for functions of the column,
    negations (`!=`, `NOT IN`, `NOT LIKE`, `NOT BETWEEN`), `IS NOT NULL` or a `LIKE` with unknown pattern; a
    leading-wildcard `LIKE` or an `ILIKE` gives a non-B-tree candidate instead: `trigram` (PostgreSQL `pg_trgm`
    GIN), `fts` (SQLite FTS5 trigram table), `fulltext` (MySQL `FULLTEXT`). A query's time is split evenly over
    its requirements.
  - **Merge.** Candidate columns = equality columns ordered by the workload time using them, then the tail. An
    index covers a requirement when its first columns are exactly the requirement's equality set followed by its
    tail (so `(a, b)` covers `(a)` and `(a, b)`). Greedy weighted set cover: repeatedly take the candidate
    covering the most still-uncovered time; `covers` lists only the fingerprints credited to it.
  - **Verification** (only with `factory(db, vendor)`, all through the always-rolled-back executor, read-only
    `SELECT`s and plain `EXPLAIN`s, never `ANALYZE`, sharing a 3 s budget): the catalog (`pg_tables`/`pg_indexes`
    in `current_schemas(false)`; `sqlite_master` + `pragma_index_list`/`pragma_index_info`/`pragma_table_info`,
    FTS5 tables by their `content=`; `information_schema.TABLES`/`STATISTICS` of `DATABASE()`) drops requirements
    an existing non-partial index prefix already serves (expression columns end the prefix; `already_indexed`
    counts them) and requirements on names that are not tables (CTEs, views). Then the top 20 remaining
    fingerprints are explained with the sample span's SQL; a `seq_scan`/`full_scan` node on the table marks it
    scanned. Without an executor the list is static analysis only (`verified: false`).
  - **est_saving_ms** = Σ over credited requirements of (time share × 0.8 when that query's EXPLAIN showed a scan
    of the table, else × 0.4). **confidence**: `high` = a scan was seen; `medium` = B-tree not explained; `low` =
    non-B-tree (not a drop-in, see `note`) or every covered query is already planned without a scan.
  - **Unused** (PostgreSQL only): `pg_stat_user_indexes` with `idx_scan = 0`, excluding primary keys, unique
    indexes and constraint-backing indexes, by `pg_relation_size` desc, ≤ 100 (counts since the last stats reset).
    Other vendors: `unused_supported: false`, `unused_error: null`. PostgreSQL without an executor:
    `unused_error.code = "explain_unavailable"` (the would-be 501), a failed query: `explain_failed`; the response
    itself stays 200.
  - **Cache.** Answers are cached per `(window, db)` for 30 s in the `RedlineAPI` instance (`cached: true`);
    `refresh=1` bypasses it. Errors: 400 `invalid_window`.

Replay is browser-side: the UI re-sends the request with `fetch` (same origin, the viewer's own cookies),
adding `X-Redline-Replay-Of: <trace id>`; the recorder turns that header into the tag `replay_of`
(like `X-Redline-Run`). Redacted values must be re-entered by the user; they are never sent as `[redacted]`.

## 13. Load tester (`redline_core.load`, extra `redline-core[load]` = httpx; console script `redline`)

- `redline load SCENARIO [--rps N | --ramp A:B | --steps A:B:STEP] [--duration 60s] [--step-duration 30s] [--workers N] [--store PATH] [--json OUT] [--fail-p95-ms X] [--fail-error-rate Y]`, `redline load init --from-openapi URL|FILE | --from-traces [--store PATH]`. Exit code 1 when a `--fail-*` threshold is breached (CI-ready).
- Scenario: JSON, TOML (3.11+) or a Python file defining `SCENARIO = {...}`:
  `{name, base_url, headers, timeout_s, requests: [{name, method, path, weight, headers, json|body, expect_status}], profile: {mode: "constant"|"ramp"|"steps", rps, start_rps, end_rps, step_rps, duration_s, step_duration_s, stop_on: {error_rate, p95_ms}}, workers}`.
- **Limits.** Every mode: fewer than 1000 steps (computed arithmetically before any plan is built), `duration_s` ≤ 24 h, rates ≤ 100 000 rps; `workers` ≤ 256, `connections` ≤ 10 000, `max_inflight` ≤ 100 000. Runs started from the UI (`launch_background`) are capped further: ≤ 200 steps, ≤ 1 h, ≤ 5 000 rps (400 `invalid_body`).
- **Process control.** The CLI marks the run `failed` (with the message) on any error once a run id exists; SIGTERM stops a run like Ctrl-C; workers exit when their parent dies (`os.getppid()` change; pid probe on Windows). `_pid_alive` uses `OpenProcess`/`GetExitCodeProcess` on Windows, `os.kill(pid, 0)` elsewhere.
- **Open model**: arrivals are scheduled at the target rate independent of responses (fixed interval or `poisson: true`); latency is measured from the *scheduled* time (coordinated-omission corrected), with a per-worker in-flight cap (beyond it: counted `dropped`). Workers are separate **processes** (asyncio + httpx each) sending per-second histograms to the parent.
- Every request carries `X-Redline-Run: <run_id>;step=<n>` so app-side traces are tagged; the run (steps, histograms) is written to the same store the app uses (`--store`, default `.redline/redline.db`) and appears in the UI.
- Knee: the first step whose p95 > 2× the first step's, or error rate > 1 %, or achieved < 90 % of target. `summary.knee_step`, `summary.max_ok_rps`.
- Run config redaction: the config kept in `runs.config` (store, `GET api/runs`, JSON results, `RunResult.config`)
  is `load.sinks.redact_config(config)`: scenario and per-request header values whose name is in `redact_headers`
  or matches `redact_keys`, secret keys of `json`/form `body`, and secrets in `base_url`/`path`/request `name` are
  masked. Only the background process sees raw values, through `runs/<id>.scenario.json`, written with mode 0600
  and removed (with the pid/stop files) when the run ends.
- Scenario generators: OpenAPI (FastAPI/Litestar/Ninja), captured traffic (store; top endpoints, concrete paths, no bodies with redacted values), Django URLconf (`manage.py redline_scenario`), Flask `url_map` (`flask redline scenario`).

## 14. Integrations (`redline_core.contrib.*`, lazy: never imported by `redline_core/__init__`)

- `contrib.sqlalchemy`: `instrument_engine(engine)` (moved from fastapi-redline, which re-exports it), `SQLAlchemyFixSuggester`, `explain_executor_factory`, plus `transaction` spans (begin/commit/rollback events) and `pool_wait` spans (checkout time ≥ 1 ms).
- `contrib.httpx` (moved), `contrib.requests`, `contrib.redis` (redis-py sync + asyncio → `cache` spans with `op` = command, `key`, `hit` for GET-like commands; credential-carrying commands – `AUTH`, `HELLO`, `CONFIG [SET]`, `MIGRATE`, `ACL [SETUSER]` – get `key` None and name = the command, e.g. `auth`), `contrib.asyncpg`, `contrib.psycopg` (psycopg 3 without an ORM → `sql` spans).
- `contrib.strawberry` (`RedlineExtension`) and `contrib.graphene` (`RedlineMiddleware`) → `resolver` spans.
- `contrib.asgi`: async helpers shared by ASGI adapters (event-loop lag monitor, Server-Timing/badge body injection).
  The lag monitor itself is zero-dependency core: `redline_core.blocking.track_request(builder)` (sync + async context manager, re-exported by `contrib.asgi`) watches the running loop while the request is in flight (no-op without a running loop, for an inert builder or with `detect_blocking` off); stalls still in progress are flushed through `TraceBuilder.on_finish` callbacks, which `finish_trace` runs first. SQL hooks of *sync* drivers call `redline_core.blocking.note_sync_call(span, call)` after recording. `redline_core.transactions.begin_transaction(builder, callsite)` / `end_transaction(state, db_alias=, outcome=)` build `transaction` spans (empty transactions and control statements such as `SAVEPOINT` are not counted/recorded).
- Core helpers used by every adapter: `TraceBuilder.server_timing() -> str` (`total;dur=…, db;dur=…;desc="12 queries", cache;dur=…, http;dur=…, tpl;dur=…, redline;desc="<trace id>"`) and `redline_core.ui.badge_snippet(trace_id, mount_url, summary) -> str` (a `<script src="<mount>/badge.js" data-…>` tag; badge.js is a dependency-free script shipped at the **root** of the bundle, from `frontend/public/`).

Concrete decisions (v0.2 implementation):

- **Badge helpers** (`redline_core.ui`): `badge_enabled(settings, debug)` (`badge` True/False as given, `"auto"` =
  the framework's debug flag: Django `DEBUG`, Flask/FastAPI/Litestar `app.debug`; always off when `enabled` is
  off); `badge_allowed(status, content_type, *, content_encoding=None, csp=None)` (2xx, `text/html`, not
  compressed, `csp_allows_badge`); `csp_allows_badge(csp)` (no policy or no `script-src`/`default-src` → yes;
  otherwise the effective `script-src` (else `default-src`) of **every** policy must contain `'self'` or `*` –
  nonce/hash-only policies skip the injection rather than cause a CSP violation); `badge_summary(builder,
  status)`; `badge_snippet(...)`; `inject_badge(body: bytes, snippet) -> bytes | None` (inserted before the
  **last** `</body>`, case-insensitive; `None` when there is none – the response is left as is);
  `BADGE_MAX_BYTES = 2 MiB` (never buffered beyond it).
- **Badge tag** (the UI's contract):
  `<script src="{mount}/badge.js" defer data-redline-trace="{trace id}" data-redline-mount="{mount}"
  data-redline-summary='{json}'></script>`, every attribute HTML-escaped. `mount` is Redline's mount as the
  browser sees it, no trailing slash (script root / ASGI `root_path` + prefix, e.g. `/_redline`,
  `/app/_redline`). `summary = {duration_ms, query_count, query_ms, issue_count: null, issue_kinds: [],
  status}` measured when the response starts: issues are only detected in `finish_trace`, so `issue_count` is
  always `null` in the tag and the badge fetches `{mount}/api/traces/{id}` (same origin, the viewer's cookies)
  when it is hovered/opened, retrying a 404 briefly (the trace is saved a moment after the response; a trace
  dropped by sampling stays 404 – show the summary only). Clicking opens `{mount}/#/requests/{id}`.
- **Where it is injected:** recorded requests only (never Redline's own URLs), status 200–299, `text/html`,
  no `Content-Encoding`, CSP permitting, not a file (Django `FileResponse`, Flask `send_file`/streamed
  responses, ASGI responses with `Content-Disposition` (both ASGI adapters) or `Last-Modified` (FastAPI), i.e.
  `FileResponse`/`StaticFiles`/`File`), not streamed (Django `StreamingHttpResponse`; Flask: only responses
  with a numeric `Content-Length` ≤ 2 MiB, never `HEAD` or the legacy `write()` API; ASGI: the start message is
  held back only when it announces such a body with `Content-Length` ≤ 2 MiB or none, and the body must be
  complete within that limit – otherwise everything passes through untouched). `Content-Length` is rewritten
  when present. Django injects only into traces that sampling kept (it finishes the trace before returning);
  the other adapters inject before the sampling decision, so the badge must tolerate a 404. The stored trace
  body is the app's own (without the badge) in Django/FastAPI/Litestar; Flask records the served body.
- **badge.js access:** Django serves top-level bundle files without the `PERMISSION` check (the file holds no
  data; the API the badge calls stays guarded); Flask/FastAPI/Litestar serve them behind the same guard as the
  UI, so a visitor the guard refuses simply gets no badge. With `token`/`basic` (§18) they are public everywhere.
- **Server-Timing:** added to every *recorded* response when `server_timing` is on, value
  `TraceBuilder.server_timing()` computed when the response starts; appended (`, `) to an existing
  `Server-Timing` header.
- **SSE** (`redline_core.stream`): `parse_since(params)` (default now; invalid → 400 `invalid_filter`),
  `sync_events(api, since)` (WSGI: generator with `time.sleep`) and `async_events(api, since,
  is_disconnected=None)` (ASGI: store polls in `asyncio.to_thread`, never blocking the loop). Headers
  `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`,
  `X-Frame-Options: DENY` (`SSE_HEADERS`). Frames: first `retry: 3000\n: connected\n\n`, then per new trace
  `event: trace\nid: <trace id>\ndata: <TraceSummary JSON>\n\n` (oldest first; the cursor advances past every
  trace sent), `: ping\n\n` every `PING_INTERVAL = 15` s, polled every `POLL_INTERVAL = 1` s, closed after
  `MAX_DURATION = 300` s (module constants, read at call time). The UI reconnects with `since` = the newest
  `started_at` it has. The stream lives under the mount, so it is never recorded and sits behind the same
  access guard as the API; between polls it holds no store lock and no app DB connection. Django picks
  `async_events` for an `ASGIRequest`, `sync_events` otherwise (a WSGI stream occupies a worker thread for up
  to 5 min); Flask uses `sync_events` **without** `stream_with_context` (the generator only reads Redline's
  store, so the app/request context – and e.g. a Flask-SQLAlchemy session – is torn down at once);
  FastAPI/Litestar stream `async_events` and stop on client disconnect.
- **base_url** (for `POST api/runs`): the app's own origin as seen by the request: `scheme://host[:port]` +
  script root / ASGI `root_path`, no trailing slash (Django `request.scheme` + `get_host()` (`ALLOWED_HOSTS`
  checked) + `SCRIPT_NAME`; Flask `request.root_url`; FastAPI/Litestar `request.base_url`). A `base_url` (and
  `workers`) in the body is dropped by the core; the run targets the `base_url` *setting* when set, else the
  adapter's `base_url` only when its host is loopback or in `allowed_hosts` (else 400 `invalid_body`, §18).
- **Run hooks:** `redline_core.load.control.run_hooks(store, settings) -> (launcher, stopper)`; `(None, None)`
  (→ 501) unless `load_from_ui` is on and the store is a `SQLiteStore` on a real file. The launcher calls
  `launch_background(config, base_url, store.path)` and pre-inserts a `running` `Run` row (the child upserts it),
  so the UI never sees a transient 404; no `base_url` → 400 `invalid_body`.
- **Route scenarios:** `redline_core.load.generate.from_routes(routes, *, store=None, base_url=None, name=,
  mount_prefix="/_redline")` over `(method, template, path | None)` triples (template = the adapter's
  `Trace.route` string): a concrete path → weight 1; a parameterised route is filled from a recent 2xx/3xx
  trace of that route when the store has one (weight 1), else left out. `generate.dump(scenario, out)` writes
  JSON for `*.json`, TOML otherwise (and to stdout).

## 15. Adapters

| package | python | framework | entry point |
|---|---|---|---|
| flask-redline (`flask_redline`) | ≥ 3.10 | Flask ≥ 3.0, < 4 | `Redline(app=None, *, engine=None, settings=None, url_prefix="/_redline", permission=None, store=None)` + `init_app(app)`; Flask-SQLAlchemy engines auto-detected; `flask redline clear\|prune\|scan\|scenario` |
| litestar-redline (`litestar_redline`) | ≥ 3.10 | Litestar ≥ 2.12, < 3 | `RedlinePlugin(settings=None, engine=None, path="/_redline", guards=None, store=None)` |

Every adapter routes every Redline request (UI, assets, top-level files, API, SSE, snapshot, login) through the core access gate (§18). FastAPI, Flask and Litestar default to `access="local"`; Django to `access="framework"` (staff / `PERMISSION`). A `dependencies` / `permission` / `guards` hook implies `access="framework"` unless `access` is set explicitly. Django Ninja is served by django-redline (route templates resolved from Ninja's operations).
Every adapter: Server-Timing, badge injection (HTML, non-streaming, 200–299, `badge` on), SSE `api/stream`, `RawResponse` passthrough, `X-Frame-Options: DENY` on Redline's own responses.
Every adapter also serves top-level bundle files (`<mount>/badge.js`, `favicon.svg`, …) from `redline_core.ui`'s
static directory (through `asset_path`, so traversal is refused), not only `assets/`, and passes `base_url` +
the `run_hooks` launcher/stopper to the API (§14). Async views are wrapped in `track_request` (FastAPI and
Litestar around the app call; Flask inside the coroutine that asgiref runs). CLI: `manage.py redline_scenario
[-o FILE]` (URLconf walk incl. Ninja/DRF routers) and `flask redline scenario [-o FILE]` (`app.url_map`), both
GET-only through `from_routes`; `flask redline scan [--json] [--fail]` (Jinja loader + blueprints).

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

## 18. Configuration and access control (`redline_core.config`, `redline_core.access`)

### Layered configuration

`redline_core.config.resolve_settings(framework, *, adapter_defaults=None, native_hook=False, environ=None,
project_root=None, read_file=True) -> ResolvedConfig` is the only way adapters build `Settings` (none calls
`Settings.from_mapping` directly any more). Layers, later wins; each value's **source** is recorded:

| source | what |
|---|---|
| `default` | `Settings` field defaults |
| `adapter` | adapter defaults (Django: `sample_rate` 0.1 without `DEBUG`, `storage_path`/`project_root` from `BASE_DIR`) and the native-hook rule below |
| `preset` | `PRESETS[preset]` |
| `file` | `REDLINE_CONFIG=<path>` (must exist), else `redline.toml`, else `pyproject.toml` with a `[tool.redline]` table – looked up in `project_root` (framework > env > adapter default > cwd), never in parent directories. `redline.toml` may be flat or nest under `[redline]`/`[tool.redline]`. TOML needs 3.11+ or `tomli` (3.10 without it: the file is ignored with a warning) |
| `env` | `REDLINE_<SETTING>` |
| `framework` | `REDLINE = {...}`, `app.config["REDLINE"]` + `Redline(settings=)`, `install(settings=)`, `RedlinePlugin(settings=)`. A `Settings` instance counts only its non-default fields as explicit |

- `preset` is read from the highest layer that sets it (framework > env > file > adapter), default `dev`. A preset only
  sets defaults; file/env/framework values win. `features` dicts merge per key across layers.
- **Native hook rule:** Django always (staff check / `PERMISSION`), FastAPI `dependencies=` (not `None`), Flask
  `permission=`, Litestar `guards=` → `access="framework"` (source `adapter`) unless `access` came from the file,
  env or framework layer (a preset does not count). So Django + `production` preset keeps the staff check.
  With an explicit other policy, FastAPI dependencies / Litestar guards still run (after the gate); a Flask
  `permission` is then unused.
- **Env parsing** by field type: bool `1/0 true/false yes/no on/off`; int/float; `""`/`none`/`null` → None for
  optional fields; lists: JSON array or comma-separated; dicts (`features`, `basic_users`): JSON object or
  `k=v,k=v` (feature values parsed as bool); `badge`/`server_timing`: `auto` or bool; `token`/`secret_key` kept
  verbatim. Every variable matching `^REDLINE_[A-Z][A-Z0-9_]*$` must name a setting (→ `SettingsError` with a "did you
  mean REDLINE_…" hint) except `REDLINE_CONFIG`, `REDLINE_ALLOW_OPEN`, `REDLINE_RELEASE` (still read by
  `resolve_release`) and `REDLINE_TEST_*`. Unknown file keys also raise (with the file path). Validation errors name
  the layer the bad value came from.
- `ResolvedConfig(settings, sources, file, warnings, env_names)`; `.public(extra_warnings)` is `GET api/config`.
  `config_warnings(settings, sources)` lists risky-but-valid combinations (open, local outside dev, wide/public
  `allowed_ips` with local, short token, no `secret_key` with token/basic, no `trusted_proxies` outside dev, bodies or
  locals explicitly on with production, `server_timing=True` outside dev, `explain_analyze` outside dev,
  `load_from_ui` with a non-local policy, retention off).

**Presets** (`redline_core.conf.PRESETS`): `dev` = the defaults (capture everything, `local`, badge/server-timing
auto). `staging` = `sample_rate` 0.25, `access="token"`, `capture_locals` False. `production` = `sample_rate` 0.05,
`always_keep_errors` True, no request/response bodies, no locals, `log_level` WARNING, `access="token"`,
`read_only` True, `badge` False, `server_timing` False, `load_from_ui` False, `explain_analyze` False. Outside `dev`
a token must be ≥ 24 characters (`SettingsError` with a generation hint); `access="token"` without a token is
always an error.

### Settings (added to `Settings`, validated like §6)

| name | type | default | meaning |
|---|---|---|---|
| preset | "dev" \| "staging" \| "production" | "dev" | see above |
| access | "local" \| "token" \| "basic" \| "framework" \| "custom" \| "open" | "local" (Django: "framework") | policy |
| access_check | callable(RequestInfo) -> bool \| dotted path (`pkg.mod:fn` or `pkg.mod.fn`) | None | `custom` |
| allowed_ips | list[str] IPs/CIDRs (v4/v6) | [] | `local`: allowed besides loopback. Other policies: when non-empty the client **must also** be in it (loopback not implicit) |
| allowed_hosts | list[str] | [] | extra `Host` names for `local`'s DNS-rebinding check (ports ignored); also accepted as run targets (below) |
| trusted_proxies | list[str] IPs/CIDRs | [] | only from these peers are `X-Forwarded-For/-Proto/-Host` believed |
| token / token_file | str \| None (secret) | None | `token`: one non-whitespace token; `token_file` read once at startup (stripped). Not both |
| basic_users | dict[user, hash] (secret) | {} | `basic`; hashes `pbkdf2_sha256$<iter>$<salt b64url>$<hash b64url>` (≥ 1000 iterations accepted, default 600 000) |
| secret_key | str ≥ 16 \| None (secret) | None | signs login cookies; unset = random per-process key (single process only) |
| read_only | bool | False | 403 `read_only` for every non-GET/HEAD/OPTIONS `api/` call except `api/explain`, `api/login`, `api/logout`; `POST api/explain` with `analyze` → 403 `read_only` (from the API) |
| features | dict[str, bool] | all True | `explain`, `profile`, `replay`, `snapshot`, `load`, `logs`, `locals`, `sse` (unknown keys → error) |
| ui_enabled | bool | True | False: every Redline URL answers 404 (capture continues) |
| mount_path | str \| None | None | FastAPI `prefix` / Flask `url_prefix` / Litestar `path` when that argument is not given (they now default to `None`); Django ignores it (the URLconf decides) |
| session_ttl_s | int ≥ 1 | 43200 | login-cookie lifetime |
| max_login_attempts | int ≥ 1 | 10 | failed credentials per client IP per 300 s |
| base_url | http(s) URL \| None | None | target of UI-started load runs (see below) |

Secrets (`SECRET_SETTINGS = token, secret_key, basic_users`) are shown as `"***"` (`basic_users` as `{user: "***"}`;
unset stays `null`) by `api/config` and `redline config`, and never logged.

### The gate (`redline_core.access`)

`RequestInfo(method, path, client_ip, headers, cookies, host, scheme, mount)` – `path` relative to the mount and
exactly what the adapter routes on; `headers` lower-cased (repeats joined `", "`); `mount` = mount path as the
browser sees it (script root / `root_path` + prefix). `RequestInfo.build(...)` normalises and parses the `Cookie`
header (repeated cookie names are all kept). `Decision(allowed, status, code, message, headers, payload, principal,
extra)`; `.body()` = the payload, or `{"error": {code, message, **extra}}`.

`Access(settings, *, framework_ok=None, environ=None, clock=time.time)` – one per adapter instance; raises
`SettingsError` at startup for a missing/unreadable/whitespace `token_file`, a weak token outside dev, an invalid
password hash, an un-importable `access_check`. Module helpers: `check(info, settings, *, framework_ok=None)`
(process-wide gate per settings object), `get_access`, `route_policy(method, path, settings)`, `hash_password`,
`verify_password`, `is_loopback`, `is_local_host`, `host_name`, `parse_cookies`.

`Access.check(info, *, framework_ok=None)` (`framework_ok` is zero-argument; exceptions from it propagate so the
adapter renders its framework's own HTTP errors), in order:

1. `ui_enabled` off → 404 `not_found`.
2. Effective client: if the TCP peer is in `trusted_proxies`, `X-Forwarded-For` is walked right-to-left skipping
   trusted hops (the first untrusted entry is the client; all trusted → leftmost; an unparsable entry is taken as the
   client and matches nothing); `X-Forwarded-Proto` (rightmost, http/https only) and `X-Forwarded-Host` (rightmost)
   replace scheme/host. From any other peer these headers are ignored. IPv6 zone ids and `::ffff:` mapping are
   normalised. `RFC 7239 Forwarded` is not parsed (only treated as a "proxied" marker).
3. Network: **local** – client loopback (or unix socket) or in `allowed_ips`, and `Host` loopback (`localhost`,
   `*.localhost`, loopback IPs, any port; missing Host accepted) or in `allowed_hosts`; and a loopback peer carrying
   `X-Forwarded-For`/`X-Real-IP`/`Forwarded` without being a trusted proxy is refused (403 `forbidden`, plus a runtime
   warning in `api/config`) – a reverse proxy on the same host would otherwise make every visitor "local". Other
   policies: `allowed_ips` as an extra requirement.
4. Policy. `open`: 403 `open_refused` unless `REDLINE_ALLOW_OPEN=1` was set when the gate was built (error logged at
   startup); otherwise allowed with a warning logged at startup and at most once a minute. `api/login` and
   `api/logout` pass to `handle()`. `token`/`basic`: the SPA shell (`""`, `assets/…`, top-level files such as
   `badge.js`) is public – it holds no data – every `api/…` needs a valid session cookie (checked first) or an
   `Authorization: Bearer <token>` / `Basic <b64>` header (scheme case-insensitive, other schemes ignored).
   Missing → 401 `login_required` (`extra.login`), wrong → 401 `invalid_credentials`; both carry `WWW-Authenticate:
   Bearer realm="Redline"` / `Basic realm="Redline", charset="UTF-8"` **unless** the request has `X-Redline-UI: 1`
   (sent by the UI and badge.js so browsers do not pop their own dialog). Tokens are compared as SHA-256 digests with
   `hmac.compare_digest`; Basic passwords with PBKDF2 (successful verifications cached 5 min under an HMAC of the
   process key; unknown users run a dummy hash). `framework`: `framework_ok()` truthy, else 403 `forbidden` (no hook
   → refused). `custom`: `access_check(info)` truthy; an exception is logged and refuses.
5. `route_policy`: a disabled feature's routes → 404 `feature_disabled` (`explain`: `api/explain`; `sse`:
   `api/stream`; `profile`: `api/traces/{id}/profile`; `snapshot`: `api/traces/{id}/snapshot`; `load`: `api/runs`
   and everything under it); `read_only` → 403 `read_only`. `RedlineAPI.dispatch` applies `route_policy` itself too.
6. CSRF, for every method but GET/HEAD/OPTIONS on every Redline URL (login and logout included):
   `Sec-Fetch-Site: cross-site` or `same-site` → 403 `csrf_failed`; an `Origin` (`null` included) whose
   `host[:port]` (default ports dropped) differs from the effective host or the `Host` header → 403 `csrf_failed`; no
   `Origin` → not a browser, allowed. It runs after the policy, so outsiders get the policy's refusal.

Rate limit: failed credentials (login body, Bearer, Basic) are counted per effective client IP in a 300 s sliding
window (in memory, per process, at most 10 000 IPs); at `max_login_attempts` further credential attempts from that IP
get 429 `too_many_requests` with `Retry-After`, until the window passes (a success resets it). Cookie-authenticated
requests are never limited. Behind a proxy without `trusted_proxies` all clients share one bucket.

Sessions: cookie `redline_session=v1.<expiry>.<b64url subject>.<b64url HMAC-SHA256>`; key = HMAC(secret_key,
"redline-session-v1") or 32 random bytes per process; the MAC covers version, expiry, subject and a binding
`sha256(policy, subject, token | user's hash)` – rotating the token or a password, or switching policy, revokes
sessions. Expired, malformed, wrongly-signed cookies or ones with an expiry beyond `now + session_ttl_s + 60` are
ignored (→ `login_required`). Attributes: `Path=<mount>` (falls back to `/` if the mount has unsafe characters),
`Max-Age=session_ttl_s`, `HttpOnly`, `SameSite=Strict`, `Secure` when the effective scheme is https. There is no
server-side session store (logout clears the cookie in that browser only; rotating `secret_key`/token revokes all).

### API (§5 rules apply)

| Method & path | Returns |
|---|---|
| `POST api/login` | body `{"token"}` (token) or `{"username", "password"}` (basic), ≤ 4 KiB → 200 `{"ok": true, "expires_at": epoch}` + `Set-Cookie`. 400 `invalid_json`/`invalid_body`, 401 `invalid_credentials`, 429 `too_many_requests`, 404 `not_found` for other policies, 405 for non-POST. Never redirects |
| `POST api/logout` | 200 `{"ok": true}` + an expiring `Set-Cookie` (any policy) |
| `GET api/config` | `{preset, file, settings: {name: value (secrets "***", callables as dotted names)}, sources: {name: source}, groups: {capture, sampling, detectors, storage, access, features, other: [names]}, warnings: [str]}` – warnings = config warnings + runtime notes of the gate |
| `GET api/meta` | `features: {explain, profile, replay, snapshot, load, logs, locals, sse, load_from_ui}` (`load_from_ui` = setting and `load` and not `read_only`), `access: {policy, login: "token"\|"basic"\|null, read_only, preset}` |

(Contract change: login/logout answer 200 JSON instead of 204, so the UI's JSON client handles them uniformly.)

`RedlineAPI(..., config=ResolvedConfig | None, access=Access | None)`. With `logs` off a trace's `logs` is `[]`
(and capture stops: the recorder installs no log handler); with `locals` off `error.frames[*].locals` is dropped
(and not captured); with `profile` off the snapshot has no profile. `replay` has no server route (UI only).

**UI-started load runs:** the target is `settings.base_url` when set; otherwise the URL the adapter derived from the
request (`Host` header) is accepted only when its host is loopback or in `allowed_hosts` (no userinfo); else
`POST api/runs` → 400 `invalid_body` asking for `base_url`. A `Host` header can no longer aim a run at another
machine.

### Adapters

All four build a `RequestInfo` per request and call the gate before any Redline handler (UI, assets, top-level
files, API, SSE, snapshot); login/logout go to `Access.handle(info, body)`; refusals use the §5 error shape with the
decision's headers (and `X-Frame-Options: DENY`). TestClient peers named `testclient` (Starlette/Litestar) count as
`127.0.0.1`.

- **FastAPI** `install(..., prefix=None, dependencies=None)`: router dependency `redline_guard(access, prefix)` first,
  then the user's dependencies. `local_only`, `loopback_guard`, `csrf_guard` remain exported for explicit use.
- **Flask** `Redline(..., url_prefix=None, permission=None)`: blueprint `before_request`; `permission(request)` is the
  framework hook (an `HTTPException` it raises is rendered as before; a False keeps the old message or the guard's
  `refusal()`).
- **Litestar** `RedlinePlugin(..., path=None, guards=None)`: router guard `redline_guard` first, then the user's guards.
- **Django**: `ALLOWED_HOSTS` check, then the gate with `PERMISSION` as the framework hook (anonymous → login
  redirect on the page, as before); top-level files (`badge.js`) skip `PERMISSION` but not the network rules. Django's
  own CSRF check still applies to API mutations (login/logout included), except requests authenticated with an
  `Authorization: Bearer` header under `access="token"`. `Set-Cookie` decisions go through `response.cookies`.

### CLI

- `redline config [--json] [--project-root DIR]` – effective config (file + env layers only; code-level settings are
  not visible), source per value (`env REDLINE_X` for env), warnings. Exit 2 on invalid configuration.
- `redline doctor [--json] [--project-root DIR]` – `ok`/`warn`/`fail` checks: storage writable, UI bundle present,
  access (open → fail, or warn with `REDLINE_ALLOW_OPEN`; local outside dev → warn), token length / `token_file`
  readable by others, basic hash strength (< 100 000 iterations → warn), `secret_key` missing with token/basic,
  `trusted_proxies` empty outside dev, production capturing bodies/locals, retention off, `load_from_ui` without
  `httpx`. Exit 1 if any check fails or the config does not load.
- `redline hash-password [--iterations N]` – prompts twice (mismatch → exit 2); reads one line from stdin when it is
  not a TTY; prints the hash.

### UI

Login screen on 401 `login_required` (token field, or username/password for basic; POSTs `./api/login` with the CSRF
header logic of `api.ts`, then reloads), "Log out" (header + command palette) when `meta.access.login` is set,
**Settings** page `#/settings` (More menu + palette) rendering `api/config` grouped with provenance chips and
warnings. `meta.features` hides Explain, Profile tab, Replay, snapshot download, Load page/new-run form, Logs tab,
locals, live SSE; `meta.access.read_only` hides Clear all, ANALYZE, run stop/delete/new. Every UI fetch (and
badge.js) sends `X-Redline-UI: 1`.
