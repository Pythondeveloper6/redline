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

Adapters plug a `FixSuggester` protocol into the detector:

```python
class FixSuggester(Protocol):
    def suggest(self, issue: Issue, spans: list[Span]) -> Suggestion | None: ...
```

## 3. Recorder (`redline_core.recorder`)

- `start_trace(**fields) -> TraceBuilder` / `finish_trace(builder, status=..., ...) -> Trace` – adapters call these.
- `current() -> TraceBuilder | None` (contextvar). Must be correct across `asyncio` tasks, `sync_to_async`, `copy_context()` threads.
- `record_span(kind, name, start, duration, data, capture_callsite=True)` – adapters' DB/cache hooks call this; it is a cheap no-op when no trace is active.
- `span(name, kind="custom", **data)` – public context manager AND decorator (sync + async) for user code. Exported as `redline_core.span`.
- Writes go through `Recorder.store.save(trace)` which must not block the request (queue + background writer thread).

## 4. Storage (`redline_core.storage`)

`Store` protocol: `save(trace)`, `get(trace_id) -> Trace | None`, `get_profile(trace_id) -> dict | None`, `list(filters, limit, cursor) -> Page`, `endpoints(window) -> list[EndpointStats]`, `issues(window) -> list[IssueGroup]`, `delete(trace_id)`, `clear()`, `prune(max_age_s, max_rows)`, `close()`.

Default `SQLiteStore(path=".redline/redline.db")`: its own file, WAL, own connection(s); never the app DB. Profiles stored in a separate table. Retention: defaults `max_rows=10_000`, `max_age=7 days`, pruned by the writer thread every N writes.

## 5. JSON HTTP API (served by adapters under their mount prefix; frontend uses relative URLs `./api/...`)

All responses JSON. Errors: `{"error": {"code": str, "message": str}}` with 4xx/5xx.

| Method & path | Returns |
|---|---|
| `GET api/meta` | `{"version", "framework", "settings": {thresholds, sampling, profiling_enabled, explain_analyze_enabled}, "storage": {"rows", "path"}}` |
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

EXPLAIN safety: only spans with `explainable: true`; executed in a transaction that is always rolled back; `analyze` refused (403 `analyze_disabled`) unless settings allow it.

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
| redact_keys | list[str] regex | (?i)pass(word)?\|secret\|token\|api[_-]?key\|auth\|session\|csrf\|card\|cvv\|ssn |
| n_plus_one_threshold | int | 5 |
| slow_query_ms | float | 100 |
| too_many_queries | int | 50 |
| profile | "off" \| "opt_in" \| "sampled" \| "all" | "opt_in" (`?_redline=profile` or header `X-Redline-Profile: 1`) |
| profile_sample_rate | float | 0.0 |
| explain_analyze | bool | False |
| max_rows / max_age_days | int | 10_000 / 7 |
| project_root | str\|None | auto (cwd) – for callsite filtering |

## 7. UI

React 19 + Vite + TS + Tailwind, source in `frontend/`, built with `base: './'` into `packages/redline-core/src/redline_core/ui/static/` (index.html + assets/). Adapters serve `index.html` for the mount root and SPA routes, and the `assets/` files. Hash routing (`#/requests/:id`) so adapters need no catch-all routes.
