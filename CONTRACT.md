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
    error: dict | None         # {"type": str, "message": str, "traceback": str} for unhandled exceptions
```

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
  - `profile_requested` is True when the adapter sees `?_redline=profile` or `X-Redline-Profile: 1`. The core starts and stops pyinstrument itself.
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
- `delete(trace_id) -> int`, `clear() -> int` and `prune(max_age_s=None, max_rows=None) -> int`, each returning the number of rows deleted
- `count() -> int`
- `flush(timeout) -> bool`
- `close()`

Default `SQLiteStore(path=".redline/redline.db", *, background=True, queue_size=1000, max_rows=10_000, max_age_s=7 days, prune_every=200)`:

- It uses its own file in WAL mode, never the app DB. Profiles are stored in a separate table.
- A daemon writer thread drains a bounded queue. When the queue is full, `save` drops the trace and increments `store.dropped`.
- The writer prunes every `prune_every` writes.
- `SQLiteStore.from_settings(settings)` builds one from `Settings`. `":memory:"` works.

`endpoints` groups by `(route or path, method)`. `series` lists only **non-empty** buckets, at most 48, where `t` is the bucket start in epoch seconds. Issue groups are deduplicated by `Issue.id`, and the sample is the latest occurrence.

## 5. JSON HTTP API (served by adapters under their mount prefix; frontend uses relative URLs `./api/...`)

All responses JSON. Errors: `{"error": {"code": str, "message": str}}` with 4xx/5xx.

Implementation: `redline_core.api.RedlineAPI(store, settings, framework=..., explain_executor=factory)`.

- `api.dispatch(method, path, params=None, body=None) -> (status, payload)`. `path` is relative to the mount, for example `"api/traces/abc"`, and leading or trailing slashes are ignored. `params` is a mapping of query parameters, where values may be str or lists. `body` is raw bytes/str or a parsed dict. Adapters just route everything under `api/` here.
- `factory(db_alias, vendor) -> executor | None`. The `executor(sql, params) -> rows` must run inside a transaction that it **always rolls back**, and must handle its own driver's paramstyle.
- Error codes:
  - 400: `invalid_filter`, `invalid_window`, `invalid_kind`, `invalid_json`, `invalid_body`, `not_explainable`, `analyze_unsupported` (MySQL)
  - 403: `analyze_disabled`
  - 404: `not_found`, `trace_not_found`, `span_not_found`, `profile_not_found`
  - 405: `method_not_allowed`
  - 422: `explain_failed`
  - 500: `internal_error`
  - 501: `explain_unavailable`
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

- Only spans with `explainable: true` can be explained. That means a single SELECT/WITH with no data-modifying CTE, `SELECT … INTO`, `FOR UPDATE` or second statement.
- EXPLAIN runs in a transaction that is always rolled back.
- `analyze` is refused (403 `analyze_disabled`) unless settings allow it.

PlanNode `flags` are `seq_scan`, `full_scan`, `sort_spill`, `hash_spill`, `nested_loop_high_rows`, `temp_sort` and `automatic_index`.

## 6. Settings (`redline_core.conf.Settings`, adapters map `REDLINE = {...}` / `install(settings=...)`)

| name | type | default |
|---|---|---|
| enabled | bool | True |
| storage_path | str | ".redline/redline.db" |
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
| max_rows / max_age_days | int | 10_000 / 7 |
| project_root | str\|None | auto (cwd) – for callsite filtering |
| writer_queue_size | int | 1_000 (background writer queue bound; overflow dropped + counted) |

`Settings.from_mapping(dict)` matches keys case-insensitively (`SAMPLE_RATE` works). Unknown keys raise `SettingsError` with a "did you mean" hint. `ignore_paths` entries that start with `^` are regular expressions; all other entries are path prefixes. Adapters append their own mount prefix.

## 7. UI

React 19 + Vite + TS + Tailwind, source in `frontend/`, built with `base: './'` into `packages/redline-core/src/redline_core/ui/static/` (index.html + assets/). Adapters serve `index.html` for the mount root and SPA routes, and the `assets/` files. Hash routing (`#/requests/:id`) so adapters need no catch-all routes.

Python helpers live in `redline_core.ui`, the package `ui/__init__.py` next to `static/`:

- `static_dir()`
- `index_html()`, which returns a placeholder page if the bundle is missing
- `asset_path(relative) -> Path | None`, which refuses traversal outside `static/`
- `content_type(path)`
- `is_built()`
