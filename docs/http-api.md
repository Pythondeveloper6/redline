# The HTTP API

[← Index](index.md)

The UI is a client of a small JSON API that lives under the same mount as the page. You
can use it yourself: to script a check, build a dashboard, or export traces.

The handlers are framework-neutral (`redline_core.api.RedlineAPI`); each adapter only
turns its request object into a call to `dispatch(method, path, params, body)` and the
`(status, payload)` tuple into a response.

## Conventions

| | |
|---|---|
| **Base** | `<mount>/api/` where `<mount>` is `/redline/` (Django) or `/_redline/` (FastAPI, Flask, Litestar) |
| **Format** | JSON in, JSON out. Responses carry `Cache-Control: no-store` |
| **Errors** | `{"error": {"code": "<code>", "message": "<text>"}}` with a 4xx or 5xx status |
| **Times** | `started_at` is a Unix epoch in seconds (float); `duration_ms`, `start_ms` are milliseconds |
| **Ids** | 32-character lowercase hex strings |
| **Limits** | `limit` is clamped to 1 to 500 |
| **Windows** | `1h`, `24h` (default), `7d`, or any `<n>m`, `<n>h`, `<n>d` (`api/history` defaults to `30d`) |
| **Status** | `status` also accepts `1xx` and `3xx` |

### Authentication and CSRF

The API sits behind the **same access control as the UI** (see [Security](security.md)).
The mutating methods are additionally protected against cross-site requests:

=== "Django"

    - Anonymous or unauthorised callers get `403` with `{"error": {"code": "forbidden", ...}}`.
    - A `Host` not in `ALLOWED_HOSTS` gets Django's own `400` before anything else.
    - The API runs Django's own CSRF check on **every** request, whether or not
      `CsrfViewMiddleware` is installed. `GET` is free; `DELETE` and `POST` need the
      token in the CSRF header (`X-CSRFToken` by default, or your `CSRF_HEADER_NAME`),
      and on HTTPS the Origin and Referer checks apply. Failure is `403` with code
      `csrf_failed`.
    - Scripts must therefore log in with a session and send the token. The page embeds a
      fresh token in `<meta name="redline-csrf">`.

=== "FastAPI"

    - Your `dependencies` run first. The default (`local_only`) answers `403
      forbidden` to a non-loopback client, or to a request whose `Host` is not a loopback
      name (DNS rebinding).
    - An `HTTPException` raised by **your** dependencies is answered in the same error
      shape: `401` becomes `unauthorized`, `403` `forbidden`, other statuses
      `bad_request`/`not_found`/`too_many_requests`/`http_error`. The exception's `detail`
      is the `message`, and its headers (`WWW-Authenticate`) are kept.
    - `DELETE` and `POST` reject browser requests whose `Origin` does not match the
      request host (or `X-Forwarded-Host`), `Origin: null`, and `Sec-Fetch-Site:
      cross-site`, with `403 csrf_failed`.
    - Requests **without** an `Origin` header (curl, scripts) pass the CSRF guard. They
      are still subject to your auth dependencies.

=== "Flask and Litestar"

    - Flask's `permission` and Litestar's `guards` run first. The default is the same
      loopback-plus-`Host` check as FastAPI. A `werkzeug` `HTTPException` (Flask) or
      Litestar exception raised by your hook is answered in the same error shape.
    - `DELETE` and `POST` get the same `Origin` / `Sec-Fetch-Site` check, with `403
      csrf_failed`.

=== "Token and basic access"

    <!-- verify: access control -->
    - With `access = "token"` or `"basic"` (see [Access control](access-control.md)) a
      caller either sends `Authorization: Bearer <token>` (or basic credentials), or logs
      in through `POST api/login` and then uses the cookie it receives.
    - An unauthenticated call is answered `401` with code `login_required`.
    - With `read_only` on, `DELETE`, `POST api/runs` and `EXPLAIN ANALYZE` answer `403
      read_only`. A switched-off feature answers `403` as well.

The examples below use curl against FastAPI on localhost. For Django, add your session
cookie and `X-CSRFToken`.

## Error codes

| Status | Code | When |
|---|---|---|
| 400 | `invalid_filter` | A list filter or cursor could not be parsed |
| 400 | `invalid_window` | `window` is not `1h`, `24h`, `7d` or `<n>m/h/d` |
| 400 | `invalid_kind` | `kind` is not one of the known issue kinds |
| 400 | `invalid_period` | A `a` or `b` of `api/history/compare` is not `release:<name>` or `range:<start>-<end>` |
| 400 | `invalid_json` | The `POST` body is not valid JSON |
| 400 | `invalid_body` | The body is not an object, or `trace_id` or `span_id` is missing or not a string |
| 400 | `not_explainable` | The span is not a single read-only `SELECT` |
| 400 | `analyze_unsupported` | `analyze: true` on MySQL |
| 400 | `unsupported_vendor` | EXPLAIN on a database that is not PostgreSQL, MySQL or SQLite |
| 401 | `unauthorized` | (FastAPI, Flask, Litestar) one of your auth hooks raised `401` |
| 401 | `login_required` | The access policy is `token` or `basic` and the request is not signed in |
| 403 | `analyze_disabled` | `analyze: true` while `explain_analyze` is off |
| 403 | `forbidden` | Django: `PERMISSION` refused. FastAPI: the loopback default refused the client or the `Host`, or your dependency raised `403` |
| 403 | `csrf_failed` | Django: the CSRF check failed. Others: a cross-origin `DELETE`/`POST` |
| 403 | `load_disabled` | `POST api/runs` while `load_from_ui` is off |
| 403 | `read_only` | A state-changing call while `read_only` is on |
| 404 | `not_found` | No such API endpoint (or UI asset) |
| 404 | `trace_not_found` | The trace id does not exist (or was pruned) |
| 404 | `span_not_found` | The span id is not in that trace |
| 404 | `profile_not_found` | The trace has no profile |
| 404 | `run_not_found` | The load-run id does not exist |
| 405 | `method_not_allowed` | Wrong method for the endpoint |
| 409 | `run_active` | `DELETE api/runs/{id}` while the run is still running |
| 429 | `too_many_requests` | Too many `POST api/login` attempts from one client |
| 422 | `explain_failed` | The database rejected the EXPLAIN |
| 500 | `internal_error` | An unexpected error. Logged on the `redline` logger |
| 501 | `explain_unavailable` | No connection or engine for the span's database alias |
| 501 | `not_supported` | The store (a custom one) cannot answer this route, or the run launcher is not wired |

Every one of these uses the `{"error": {"code", "message"}}` shape, on both adapters. The
only exception is Django's own `400` for a disallowed `Host`, which Django renders before
Redline runs.

## Endpoints

### GET api/meta

Version, framework, effective settings and storage state. The UI uses it for the version
label; it is also a cheap health check.

```bash
curl -s localhost:8000/_redline/api/meta
```

```json
{
  "version": "0.2.0a1",
  "framework": "fastapi",
  "settings": {
    "thresholds": {"n_plus_one": 5, "duplicate": 2, "slow_query_ms": 100.0, "too_many_queries": 50},
    "sampling": {"sample_rate": 1.0, "always_keep_slower_than_ms": 500.0, "always_keep_errors": true},
    "profiling_enabled": true,
    "profile_mode": "opt_in",
    "explain_analyze_enabled": false,
    "profiler_available": true
  },
  "storage": {"rows": 143, "path": ".redline/redline.db", "dropped": 0},
  "project_root": "/srv/shop",
  "editor": "vscode",
  "editor_url_template": "vscode://file/{file}:{line}",
  "release": "3f9c2ab",
  "features": {"load_from_ui": false, "sse": true, "snapshot": true}
}
```

The last five keys are new in 0.2: `project_root` and the editor fields drive the
[open-in-editor](the-ui.md#open-in-editor) links, `release` is the resolved
[release](history.md#releases) name, and `features` tells the UI which optional parts exist.

`storage.dropped` counts traces dropped because the writer queue was full. A value that
keeps rising means the writer cannot keep up: see [Storage](storage.md#the-writer-thread).

### GET api/traces

The paginated request list, newest first.

| Parameter | Meaning |
|---|---|
| `limit` | Page size, default 50, clamped to 1..500 |
| `cursor` | The `next_cursor` of the previous page |
| `q` | Case-insensitive substring of path, route or view |
| `method` | An HTTP method (upper-cased for you) |
| `status` | `1xx` to `5xx`, or an exact 3-digit code |
| `min_ms` | Duration of at least this many milliseconds |
| `min_queries` | At least this many SQL queries |
| `has_issues` | `1`, `true`, `yes` or `on` for traces with at least one issue |
| `route` | The exact route template, e.g. `/orders/{order_id}` |
| `tag` | `key:value`, e.g. `run_id:abc123` |

```bash
curl -s "localhost:8000/_redline/api/traces?status=5xx&limit=2"
```

```json
{
  "items": [
    {
      "id": "5b1f0c8e2d3a4f6b9c7d8e9f0a1b2c3d",
      "method": "GET",
      "path": "/boom",
      "route": "/boom",
      "view": "shop.api.boom",
      "status": 500,
      "started_at": 1761490120.412,
      "duration_ms": 1.043,
      "query_count": 1,
      "query_ms": 0.312,
      "issue_count": 0,
      "issue_kinds": [],
      "has_profile": false,
      "tags": {"sampled_by": "all"}
    }
  ],
  "next_cursor": null,
  "total": 1
}
```

`total` is the number of **matching** traces, not the page size. The cursor is opaque
(base64 of the last row's start time and id), so keep it stable: pages stay correct while
new traces arrive at the top.

### GET api/traces/{id}

The full trace, without its profile (`has_profile` tells you whether one exists).

```bash
curl -s localhost:8000/_redline/api/traces/5b1f0c8e2d3a4f6b9c7d8e9f0a1b2c3d
```

```json
{
  "id": "5b1f0c8e2d3a4f6b9c7d8e9f0a1b2c3d",
  "framework": "fastapi",
  "method": "GET",
  "path": "/orders",
  "route": "/orders",
  "view": "shop.api.list_orders",
  "status": 200,
  "started_at": 1761490120.412,
  "duration_ms": 22.7,
  "query_count": 31,
  "query_ms": 9.8,
  "request": {
    "headers": {"host": "localhost:8000", "authorization": "[redacted]"},
    "query": {},
    "body": null,
    "body_truncated": false,
    "client": "127.0.0.1"
  },
  "response": {"headers": {"content-type": "application/json"}, "body": "[...]", "body_truncated": false, "size": 3921},
  "spans": [
    {
      "id": "0a1b...", "parent_id": null, "kind": "sql",
      "name": "SELECT \"id\", \"order_id\" FROM \"order_items\" WHERE \"order_id\" = ?",
      "start_ms": 1.9, "duration_ms": 0.31,
      "callsite": {"file": "shop/api.py", "line": 44, "function": "list_orders",
                   "code": "items = await session.scalars(...)",
                   "stack": [{"file": "shop/api.py", "line": 44, "function": "list_orders"}]},
      "data": {"sql": "SELECT ...", "params": [17], "fingerprint": "SELECT ...", "db_alias": "default",
               "vendor": "sqlite", "rows": 3, "many": false, "explainable": true}
    }
  ],
  "issues": [
    {
      "id": "e3f1a2b4c5d6e7f8", "kind": "n_plus_one", "severity": "critical",
      "title": "N+1: 30 queries on order_items from shop/api.py:44",
      "detail": "The same query ran 30 times from one line of code, ...",
      "span_ids": ["0a1b...", "..."], "fingerprint": "SELECT ...",
      "callsite": {"file": "shop/api.py", "line": 44, "function": "list_orders", "code": "...", "stack": []},
      "count": 30, "total_ms": 9.2,
      "suggestion": {"summary": "Eager-load Order.items (collection) with selectinload() ...",
                     "code": "select(Order).options(selectinload(Order.items))",
                     "diff": "--- a/shop/api.py\n+++ b/shop/api.py\n...",
                     "confidence": "high", "docs_url": "https://docs.sqlalchemy.org/..."}
    }
  ],
  "tags": {"sampled_by": "all"},
  "error": null,
  "has_profile": false
}
```

(Values are illustrative and shortened; the shape is exact.) `error` is `null` or
`{"type", "message", "traceback"}`.

### GET api/traces/{id}/profile

The trace's flamegraph as [speedscope](https://github.com/jlfwong/speedscope) JSON, or
`404 profile_not_found`.

```bash
curl -s localhost:8000/_redline/api/traces/<id>/profile -o profile.speedscope.json
```

### DELETE api/traces/{id}

Delete one trace with its profile, tags and issue rows.

```bash
curl -s -X DELETE localhost:8000/_redline/api/traces/<id>
# {"deleted": 1}
```

Deleting an id that does not exist is not an error: `{"deleted": 0}`.

### DELETE api/traces

Delete **everything**. The UI's **Clear all** calls this.

```bash
curl -s -X DELETE localhost:8000/_redline/api/traces
# {"deleted": 143}
```

### GET api/endpoints

Per-route aggregates. Parameter: `window`.

```bash
curl -s "localhost:8000/_redline/api/endpoints?window=1h"
```

```json
{
  "items": [
    {
      "route": "/orders/{order_id}", "method": "GET", "count": 412, "error_rate": 0.0,
      "p50_ms": 2.6, "p95_ms": 8.6, "p99_ms": 9.5,
      "avg_queries": 2.0, "max_queries": 2, "avg_query_ms": 0.9, "issue_count": 0,
      "series": [{"t": 1761486600.0, "p95_ms": 7.9, "count": 38}]
    }
  ]
}
```

Definitions are on [Issues and endpoints](issues-and-endpoints.md#endpoints).

### GET api/issues

Deduplicated issues. Parameters: `window`, `kind`.

```bash
curl -s "localhost:8000/_redline/api/issues?kind=n_plus_one"
```

```json
{
  "items": [
    {
      "id": "e3f1a2b4c5d6e7f8", "kind": "n_plus_one", "severity": "critical",
      "title": "N+1: 30 queries on order_items from shop/api.py:44",
      "detail": "The same query ran 30 times ...",
      "occurrences": 17, "first_seen": 1761488000.1, "last_seen": 1761490120.4,
      "routes": ["/orders"],
      "sample_trace_id": "5b1f0c8e2d3a4f6b9c7d8e9f0a1b2c3d",
      "sample_span_ids": ["0a1b..."],
      "suggestion": {"summary": "...", "code": "...", "diff": "...", "confidence": "high", "docs_url": "..."},
      "callsite": {"file": "shop/api.py", "line": 44, "function": "list_orders", "code": "...", "stack": []}
    }
  ]
}
```

### POST api/explain

Run EXPLAIN for one recorded query.

```bash
curl -s -X POST localhost:8000/_redline/api/explain \
  -H 'content-type: application/json' \
  -d '{"trace_id": "5b1f0c8e2d3a4f6b9c7d8e9f0a1b2c3d", "span_id": "0a1b...", "analyze": false}'
```

| Body field | Type | Meaning |
|---|---|---|
| `trace_id` | string, required | The trace |
| `span_id` | string, required | A `sql` span of that trace |
| `analyze` | boolean, default `false` | Run `EXPLAIN ANALYZE`. Needs `explain_analyze` on (`403 analyze_disabled` otherwise) |

Response, `200`:

```json
{
  "vendor": "sqlite",
  "analyzed": false,
  "plan": [[2, 0, 0, "SCAN audit_log"]],
  "text": "SCAN audit_log  [full_scan]",
  "tree": {"label": "SCAN audit_log", "detail": "", "cost": null, "rows": null,
           "actual_ms": null, "children": [], "flags": ["full_scan"]},
  "findings": [{"severity": "warning", "title": "Full table scan of audit_log", "detail": "..."}],
  "suggested_indexes": ["CREATE INDEX audit_log_message_idx ON audit_log (message);"]
}
```

Field-by-field semantics and the safety model are on [EXPLAIN](explain.md).

### GET api/traces: new filters

0.2 adds three parameters to the list endpoint:

| Parameter | Meaning |
|---|---|
| `sql=<text>` | only traces that ran a statement whose **normalised** SQL contains the text (case-insensitive) |
| `since=<epoch>` | only traces strictly newer than this time |
| `replay_of=<trace id>` | only [replays](the-ui.md#replay) of that trace |

An invalid value is `400 invalid_filter`.

### GET api/stream

<!-- verify: SSE api/stream -->
A **Server-Sent Events** feed of new traces, used by [Live mode](the-ui.md#live-mode).
Parameter: `since=<epoch>` (default: now).

```bash
curl -N "localhost:8000/_redline/api/stream"
```

```text
event: trace
data: {"id": "9d4c...", "method": "GET", "path": "/orders/", "status": 200, "duration_ms": 84.2, ...}

: ping
```

Each `trace` event carries a `TraceSummary`. A comment line `: ping` is sent every 15
seconds as a keep-alive, and the server closes the stream after five minutes; the client
reconnects with `since=` set to the last trace it saw. It is served by the adapter and is
subject to the same access control.

### GET api/traces/{id}/snapshot

<!-- verify: snapshot download -->
The trace as a **standalone HTML page** (`text/html`, with `Content-Disposition: attachment;
filename="redline-<id>.html"`): the UI bundle inlined, with the trace and its profile
embedded. Open it offline. See [Snapshots](the-ui.md#snapshots).

### GET api/compare

Two traces side by side. Parameters: `a` and `b` (trace ids).

```bash
curl -s "localhost:8000/_redline/api/compare?a=5b1f...&b=9d4c..."
```

```json
{
  "a": {"id": "5b1f...", "duration_ms": 412.0, "query_count": 31, "query_ms": 380.2},
  "b": {"id": "9d4c...", "duration_ms": 38.4,  "query_count": 3,  "query_ms": 21.0},
  "delta": {"duration_ms": -373.6, "query_count": -28, "query_ms": -359.2},
  "by_kind": {"sql": {"a_ms": 380.2, "b_ms": 21.0, "a_count": 31, "b_count": 3}},
  "sql": [{"fingerprint": "8a3c0f21d4b6e7a9", "sql": "SELECT ... WHERE customer_id = ?",
           "a_count": 30, "b_count": 0, "a_ms": 360.0, "b_ms": 0.0}],
  "issues": {"only_a": [{"id": "e3f1...", "kind": "n_plus_one", "severity": "critical", "title": "..."}],
             "only_b": [], "both": []}
}
```

`delta` and every `*_ms` difference are **b minus a**. The `sql` rows are keyed by the
16-hex fingerprint and sorted by the largest time difference. A missing `a` or `b` is `400
invalid_filter`; an unknown id is `404 trace_not_found`.

### GET api/sql

SQL aggregated by fingerprint across traces. Parameters: `q` (substring of the normalised
SQL), `window` (default `24h`), `limit` (default 100).

```json
{"items": [{"fingerprint": "8a3c0f21d4b6e7a9", "sql": "SELECT ... FROM shop_order WHERE status = ?",
            "count": 1840, "traces": 412, "total_ms": 5120.4, "max_ms": 88.0, "mean_ms": 2.78,
            "mean_rows": 14.2, "routes": ["GET /orders/"], "sample_trace_id": "9d4c...",
            "vendor": "postgresql"}]}
```

Sorted by total time. See [Database](database.md).

### GET api/indexes

<!-- verify: index advisor (api/indexes) -->
The workload index advisor and the unused-index list. Parameter: `window`.

```json
{
  "candidates": [{
    "table": "shop_order", "columns": ["customer_id", "status"],
    "create_sql": "CREATE INDEX shop_order_customer_id_status_idx ON shop_order (customer_id, status);",
    "covers": ["8a3c0f21d4b6e7a9"], "queries": 1840, "total_ms": 5120.4,
    "est_saving_ms": 4300.0, "vendor": "postgresql"
  }],
  "unused": [{"table": "shop_order", "index": "shop_order_legacy_idx", "scans": 0, "size_bytes": 8388608}],
  "unused_supported": true
}
```

Answers `501 not_supported` if the store cannot provide the workload. See
[Database](database.md#index-advisor).

### GET api/cache

Cache efficiency per backend and key pattern. Parameter: `window`.

```json
{"items": [{"backend": "redis", "key_pattern": "user:*:profile", "gets": 5200, "hits": 4100,
            "misses": 1100, "hit_rate": 0.788, "sets": 1100, "deletes": 12,
            "unread_sets": 0, "stampedes": 2}]}
```

`hit_rate` is `null` when the backend does not report hits. See
[Cache efficiency](cache-efficiency.md).

### GET api/external

Outbound HTTP per method, host and URL pattern. Parameter: `window`.

```json
{"items": [{"method": "POST", "host": "api.payments.example", "url_pattern": "https://api.payments.example/v2/charge",
            "count": 310, "total_ms": 91200.0, "p95_ms": 840.0, "max_ms": 2100.0,
            "errors": 6, "error_rate": 0.019, "duplicates": 0, "retries": 1}]}
```

See [External calls](external-calls.md).

### GET api/history

Time series of an endpoint, or of the whole app when `route` is omitted. Parameters:
`route`, `method` (case-insensitive), `window` (default `30d`), `bucket` (`1h` or `1d`;
default `1h` for windows up to two days, else `1d`; anything else is `400 invalid_filter`).

```json
{"series": [{"t": 1761436800, "count": 4120, "p50_ms": 11.0, "p95_ms": 48.0, "p99_ms": 120.0,
             "error_rate": 0.002, "avg_queries": 4.1}],
 "releases": [{"release": "3f9c2ab", "first_seen": 1761440400}]}
```

Only non-empty buckets are returned. See [History](history.md).

### GET api/releases

```json
{"items": [{"release": "3f9c2ab", "first_seen": 1761440400, "last_seen": 1761526800, "requests": 88210}]}
```

### GET api/history/compare

Compare two periods. `a` is the baseline and `b` the period under test; each is
`release:<name>` or `range:<start>-<end>` (epoch seconds). An invalid one is `400
invalid_period`.

```bash
curl -s "localhost:8000/_redline/api/history/compare?a=release:a81d0e4&b=release:3f9c2ab"
```

```json
{
  "a": {"kind": "release", "release": "a81d0e4", "requests": 61000},
  "b": {"kind": "release", "release": "3f9c2ab", "requests": 88210},
  "endpoints": [{
    "route": "/orders/", "method": "GET",
    "a": {"count": 4100, "error_rate": 0.0, "p50_ms": 12.0, "p95_ms": 40.0, "p99_ms": 90.0, "avg_ms": 18.0, "avg_queries": 4.0},
    "b": {"count": 5200, "error_rate": 0.0, "p50_ms": 30.0, "p95_ms": 95.0, "p99_ms": 210.0, "avg_ms": 41.0, "avg_queries": 31.0},
    "delta_p95_pct": 137.5, "delta_queries": 27.0, "verdict": "regressed"
  }],
  "issues": {"new": [{"id": "e3f1...", "kind": "n_plus_one", "severity": "critical", "title": "...",
                      "route": "/orders/", "first_seen": 1761440500, "last_seen": 1761526000,
                      "first_release": "3f9c2ab", "last_release": "3f9c2ab", "occurrences": 17}],
             "resolved": [], "persisting": []}
}
```

An endpoint's `a` or `b` is `null` when it is new or gone, and `delta_p95_pct` is `null`
when it cannot be computed. Verdicts and sort order are explained in
[History](history.md#before-and-after-comparing-periods): regressed means p95 up 20% **and**
5 ms, or average queries up by 1.

### Runs

Load-test runs ([Load testing](load-testing.md)).

| Request | Returns |
|---|---|
| `GET api/runs?limit=50` | `{"items": [Run]}`, newest first, steps included |
| `GET api/runs/{id}` | one run |
| `GET api/runs/{id}/bottlenecks` | what got slower, per step |
| `POST api/runs` | start a run (`202`) |
| `POST api/runs/{id}/stop` | stop a run: `{"stopped": true}` |
| `DELETE api/runs/{id}` | delete a finished run: `{"deleted": 1}` |

A `Run`:

```json
{
  "id": "7c1e...", "name": "browse", "status": "done",
  "config": {"workers": 4},
  "created_at": 1761500000, "finished_at": 1761500220,
  "steps": [{"step": 1, "target_rps": 10, "achieved_rps": 10.0, "sent": 200, "ok": 200, "errors": 0,
             "dropped": 0, "p50_ms": 12.0, "p95_ms": 21.0, "p99_ms": 38.0, "max_ms": 64.0,
             "error_rate": 0.0, "error_breakdown": {}}],
  "summary": {"knee_step": 8, "max_ok_rps": 69.8, "verdict": "saturated"}
}
```

`status` is `running`, `done`, `stopped` or `failed`. The knee rule and verdicts are on the
[Load testing](load-testing.md#reading-the-results) page.

`GET api/runs/{id}/bottlenecks`:

```json
{
  "steps": [{"step": 1, "target_rps": 10}, {"step": 8, "target_rps": 80}],
  "items": [{
    "key": "sql:8a3c0f21d4b6e7a9", "kind": "sql", "label": "SELECT ... FROM shop_order WHERE ...",
    "callsite": {"file": "shop/api.py", "line": 44, "function": "list_orders"},
    "per_step": [{"step": 1, "mean_ms": 3.1, "p95_ms": 5.0, "count": 40},
                 {"step": 8, "mean_ms": 410.0, "p95_ms": 900.0, "count": 310}],
    "growth": 132.3
  }]
}
```

`kind` is `sql` or `route`. Items are sorted by `growth` (last step's mean over the first's),
top 50. It is `404 run_not_found` when there is neither the run nor a trace tagged with it.

`POST api/runs` starts a run **against the app that serves the UI**:

<!-- verify: load from UI (POST api/runs) -->
```bash
curl -s -X POST localhost:8000/_redline/api/runs -H 'content-type: application/json' -d '{
  "name": "smoke",
  "requests": [{"method": "GET", "path": "/api/orders/", "weight": 3}],
  "profile": {"mode": "steps", "start_rps": 10, "end_rps": 50, "step_rps": 10, "step_duration_s": 20}
}'
# 202 {"id": "7c1e...", "name": "smoke", "status": "running"}
```

It is refused with `403 load_disabled` unless `load_from_ui` is on (and `403 read_only` in
read-only mode). Every `path` must start with a single `/`: a scheme or host is
`400 invalid_body`, so the target can only be the app itself. See
[Security](security.md#load-from-the-ui).

### POST api/login, POST api/logout, GET api/config

<!-- verify: access control (api/login, api/config) -->
Part of [access control](access-control.md#login-and-api).

| Request | Returns |
|---|---|
| `POST api/login` body `{"token": "..."}` or `{"username": "...", "password": "..."}` | `204` and a session cookie (HttpOnly, SameSite=Strict, path-scoped) |
| `POST api/logout` | clears the cookie |
| `GET api/config` | the effective configuration, secrets as `"***"`, with where each value came from (default, preset, file, env, framework) and a list of warnings |

Too many login attempts is `429 too_many_requests`.

## Things to know

- **The API is not versioned.** It is `0.2.0a1`: the shape may change between alpha
  releases. The frontend and the core ship together, so they never disagree.
- **Redacted values are redacted in the API too.** There is no way to ask for the
  original.
- **Anything you can read here is on disk.** The API reads the same store. A leaked
  session on this API is a leak of recent request bodies. See [Security](security.md).
- **Polling is cheap but not free.** The list endpoint runs a `COUNT(*)` for `total` on
  every call. Prefer `api/stream` for a live view; the UI falls back to polling every two
  seconds only when streaming is unavailable.
- **A store may not support every route.** A custom store without the extended methods
  answers `501 not_supported` for the history, SQL, cache, external and run routes. The core
  routes keep working.
