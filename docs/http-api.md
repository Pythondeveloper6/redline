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
| **Base** | `<mount>/api/` where `<mount>` is `/redline/` (Django) or `/_redline/` (FastAPI) |
| **Format** | JSON in, JSON out. Responses carry `Cache-Control: no-store` |
| **Errors** | `{"error": {"code": "<code>", "message": "<text>"}}` with a 4xx or 5xx status |
| **Times** | `started_at` is a Unix epoch in seconds (float); `duration_ms`, `start_ms` are milliseconds |
| **Ids** | 32-character lowercase hex strings |
| **Limits** | `limit` is clamped to 1 to 500 |
| **Windows** | `1h`, `24h` (default), `7d`, or any `<n>m`, `<n>h`, `<n>d` |

### Authentication and CSRF

The API sits behind the **same access control as the UI** (see [Security](security.md)).
The mutating methods are additionally protected against cross-site requests:

=== "Django"

    - Anonymous or unauthorised callers get `403` with `{"error": {"code": "forbidden", ...}}`.
    - The API runs Django's own CSRF check on **every** request, whether or not
      `CsrfViewMiddleware` is installed. `GET` is free; `DELETE` and `POST` need the
      token in the CSRF header (`X-CSRFToken` by default, or your `CSRF_HEADER_NAME`),
      and on HTTPS the Origin and Referer checks apply. Failure is `403` with code
      `csrf_failed`.
    - Scripts must therefore log in with a session and send the token. The page embeds a
      fresh token in `<meta name="redline-csrf">`.

=== "FastAPI"

    - Your `dependencies` run first. The default (`local_only`) answers `403` to a
      non-loopback client with FastAPI's usual `{"detail": "..."}` body, **not** the error
      shape above.
    - `DELETE` and `POST` reject browser requests whose `Origin` does not match the
      request host (or `X-Forwarded-Host`), `Origin: null`, and `Sec-Fetch-Site:
      cross-site`, with `403 {"detail": "Cross-origin request blocked."}`.
    - Requests **without** an `Origin` header (curl, scripts) pass the CSRF guard. They
      are still subject to your auth dependencies.

The examples below use curl against FastAPI on localhost. For Django, add your session
cookie and `X-CSRFToken`.

## Error codes

| Status | Code | When |
|---|---|---|
| 400 | `invalid_filter` | A list filter or cursor could not be parsed |
| 400 | `invalid_window` | `window` is not `1h`, `24h`, `7d` or `<n>m/h/d` |
| 400 | `invalid_kind` | `kind` is not one of the four issue kinds |
| 400 | `invalid_json` | The `POST` body is not valid JSON |
| 400 | `invalid_body` | The body is not an object, or `trace_id` or `span_id` is missing or not a string |
| 400 | `not_explainable` | The span is not a single read-only `SELECT` |
| 400 | `analyze_unsupported` | `analyze: true` on MySQL |
| 400 | `unsupported_vendor` | EXPLAIN on a database that is not PostgreSQL, MySQL or SQLite |
| 403 | `analyze_disabled` | `analyze: true` while `explain_analyze` is off |
| 403 | `forbidden` | (Django) no permission |
| 403 | `csrf_failed` | (Django) CSRF check failed |
| 404 | `not_found` | No such API endpoint |
| 404 | `trace_not_found` | The trace id does not exist (or was pruned) |
| 404 | `span_not_found` | The span id is not in that trace |
| 404 | `profile_not_found` | The trace has no profile |
| 405 | `method_not_allowed` | Wrong method for the endpoint |
| 422 | `explain_failed` | The database rejected the EXPLAIN |
| 500 | `internal_error` | An unexpected error. Logged on the `redline` logger |
| 501 | `explain_unavailable` | No connection or engine for the span's database alias |

## Endpoints

### GET api/meta

Version, framework, effective settings and storage state. The UI uses it for the version
label; it is also a cheap health check.

```bash
curl -s localhost:8000/_redline/api/meta
```

```json
{
  "version": "0.1.0a1",
  "framework": "fastapi",
  "settings": {
    "thresholds": {"n_plus_one": 5, "duplicate": 2, "slow_query_ms": 100.0, "too_many_queries": 50},
    "sampling": {"sample_rate": 1.0, "always_keep_slower_than_ms": 500.0, "always_keep_errors": true},
    "profiling_enabled": true,
    "profile_mode": "opt_in",
    "explain_analyze_enabled": false,
    "profiler_available": true
  },
  "storage": {"rows": 143, "path": ".redline/redline.db", "dropped": 0}
}
```

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

## Things to know

- **The API is not versioned.** It is `0.1.0a1`: the shape may change between alpha
  releases. The frontend and the core ship together, so they never disagree.
- **Redacted values are redacted in the API too.** There is no way to ask for the
  original.
- **Anything you can read here is on disk.** The API reads the same store. A leaked
  session on this API is a leak of recent request bodies. See [Security](security.md).
- **Polling is cheap but not free.** The list endpoint runs a `COUNT(*)` for `total` on
  every call. The UI polls every two seconds while the Requests page is open and not
  paused.
