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
      `CsrfViewMiddleware` is installed. `GET` is free; `DELETE`, `PUT` and `POST` need the
      token in the CSRF header (`X-CSRFToken` by default, or your `CSRF_HEADER_NAME`),
      and on HTTPS the Origin and Referer checks apply. Failure is `403` with code
      `csrf_failed`.
    - Scripts must therefore log in with a session and send the token. The page embeds a
      fresh token in `<meta name="redline-csrf">`. The exception is `access = "token"`: a
      request authenticated with an `Authorization: Bearer` header skips Django's CSRF
      check.

=== "FastAPI"

    - Redline's access check runs first, then your `dependencies`. The default policy
      (`local`) answers `403 forbidden` to a non-loopback client, or to a request whose
      `Host` is not a loopback name (DNS rebinding).
    - An `HTTPException` raised by **your** dependencies is answered in the same error
      shape: `401` becomes `unauthorized`, `403` `forbidden`, other statuses
      `bad_request`/`not_found`/`too_many_requests`/`http_error`. The exception's `detail`
      is the `message`, and its headers (`WWW-Authenticate`) are kept.

=== "Flask and Litestar"

    - Redline's access check runs first, then Flask's `permission` or Litestar's `guards`
      (passing one implies `access = "framework"`). A `werkzeug` `HTTPException` (Flask) or
      Litestar exception raised by your hook is answered in the same error shape.

=== "Token and basic access"

    - With `access = "token"` or `"basic"` (see [Access control](access-control.md)) a
      caller either sends `Authorization: Bearer <token>` (or `Authorization: Basic ...`),
      or logs in through `POST api/login` and then uses the cookie it receives. The page
      shell and assets are public; **every `api/` call needs credentials**.
    - An unauthenticated call is answered `401` with code `login_required` (wrong
      credentials: `invalid_credentials`). Both carry a `WWW-Authenticate` header unless
      the request sends `X-Redline-UI: 1`, which the UI and the badge do so browsers show
      no native dialog.
    - With `read_only` on, every non-GET `api/` call except `api/explain`, `api/login` and
      `api/logout` answers `403 read_only`, and so does `POST api/explain` with
      `"analyze": true`.
    - A switched-off [feature](access-control.md#feature-switches) answers
      `404 feature_disabled`.

**CSRF, on every framework.** For every method but `GET`, `HEAD` and `OPTIONS` (login and
logout included) a request is refused with `403 csrf_failed` when it carries
`Sec-Fetch-Site: cross-site` **or `same-site`**, or an `Origin` (including `null`) whose
host and port differ from the host the request was addressed to (`X-Forwarded-Host` when
the peer is a trusted proxy). A request **without** an `Origin` (curl, scripts) passes this
check and is subject to the access policy only.

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
| 400 | `invalid_suite` | The suite of an `api/checks` call is not valid: unknown key, value out of range, a write method, an absolute URL, or over the run caps (200 requests, 5 000 calls) |
| 400 | `invalid_baseline` | The body of `POST api/checks/baselines/import` is not a `redline-check-baseline` document (format, version or shape) |
| 400 | `invalid_body` | The body is not an object, or a required field is missing or not valid (for `POST api/runs`: also when no load target can be determined, see [Runs](#runs)) |
| 400 | `not_explainable` | The span is not a single read-only `SELECT` |
| 400 | `analyze_unsupported` | `analyze: true` on MySQL |
| 400 | `unsupported_vendor` | EXPLAIN on a database that is not PostgreSQL, MySQL or SQLite |
| 401 | `unauthorized` | (FastAPI, Flask, Litestar) one of your auth hooks raised `401` |
| 401 | `login_required` | The access policy is `token` or `basic` and the request is not signed in |
| 403 | `analyze_disabled` | `analyze: true` while `explain_analyze` is off |
| 401 | `invalid_credentials` | Wrong token, password or username |
| 403 | `forbidden` | The policy refused: not a loopback client or `Host` under `local`, a client outside `allowed_ips`, a loopback peer with forwarding headers and no `trusted_proxies`, `framework` with a hook that returned false (or no hook), `custom` returned false, or your dependency raised `403` |
| 403 | `open_refused` | `access = "open"` without `REDLINE_ALLOW_OPEN=1` |
| 403 | `csrf_failed` | The CSRF check failed (see above; on Django also Django's own check) |
| 403 | `load_disabled` | `POST api/runs` while `load_from_ui` is off |
| 403 | `checks_from_ui_disabled` | `POST api/checks/runs` while `checks_from_ui` is off (or `auto` outside debug mode) |
| 403 | `read_only` | A state-changing call while `read_only` is on |
| 404 | `feature_disabled` | The route belongs to a feature switched off in `features` |
| 404 | `not_found` | No such API endpoint (or UI asset), or `ui_enabled` is off, or `api/login` under a policy without login |
| 404 | `trace_not_found` | The trace id does not exist (or was pruned) |
| 404 | `span_not_found` | The span id is not in that trace |
| 404 | `profile_not_found` | The trace has no profile |
| 404 | `run_not_found` | The load-run id does not exist |
| 404 | `suite_not_found` | The check-suite id does not exist |
| 404 | `check_run_not_found` | The check-run id (or the `baseline_id` of a compare) does not exist |
| 405 | `method_not_allowed` | Wrong method for the endpoint |
| 409 | `run_active` | `DELETE api/runs/{id}` or `DELETE api/checks/runs/{id}` while the run is still running; `DELETE api/checks/suites/{id}` while one of its runs is; `POST api/checks/runs` while a run of the same suite is |
| 413 | `payload_too_large` | A suite body over 1 MiB, or a baseline import over 10 MiB |
| 429 | `too_many_requests` | Too many failed credentials from one client (`Retry-After` is set) |
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
| `q` | Case-insensitive substring of path, route, view or configured request/user/job ID tags |
| `method` | An HTTP method or `TASK` (upper-cased for you) |
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

Per-route aggregates. Parameters: `window`, `group_by` (optional).

`group_by=blueprint` (Flask) or `group_by=tag:<key>` aggregates by a trace tag instead of
by route: one item per tag value with `route` set to the value, `method` `"*"` and
`routes` counting the distinct routes inside it; untagged requests are `"(none)"`. The
response then also carries `"group_by"`. An invalid value answers 400
(`invalid_group_by`).

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

A **Server-Sent Events** feed of new traces, used by [Live mode](the-ui.md#live-mode).
Parameters: `since=<epoch>` (default: now) and `after=<seq>`. Every trace has a `seq`, its
position in insertion order, and every event carries it as the SSE `id`. A client that
reconnects resumes exactly after the last event it saw with `after=<seq>` or the standard
`Last-Event-ID` header, so a slow request that finished late is never skipped. `after`
wins over `since`.

```bash
curl -N "localhost:8000/_redline/api/stream"
```

```text
retry: 3000
: connected

event: trace
id: 1842
data: {"id": "9d4c...", "seq": 1842, "method": "GET", "path": "/orders/", "status": 200, "duration_ms": 84.2, ...}

: ping
```

Each `trace` event carries a `TraceSummary`. The stream opens with a `retry:` hint (3000 ms on ASGI, 500 ms on WSGI) and
a `: connected` comment, polls the store every second, and sends a comment line `: ping`
every 15 seconds as a keep-alive. The server closes the stream so the browser reconnects:
after **five minutes** on ASGI servers (FastAPI, Litestar) and after about **25 seconds**
on WSGI servers (Flask, Django), where each open stream holds a worker thread. The UI
reconnects with the last `seq`, so nothing is lost. It is served by the adapter, is subject
to the same access control, and can be switched off with the `sse` feature
(`404 feature_disabled`).

### GET api/traces/{id}/snapshot

The trace as a **standalone HTML page** (`text/html; charset=utf-8`, with
`Content-Disposition: attachment; filename="redline-<id>.html"`): the UI bundle inlined,
with the trace (as stored, already redacted) and its profile embedded. The profile is left
out when the `profile` feature is off; the route answers `404 feature_disabled` when
`snapshot` is off, and `404 trace_not_found` for an unknown id. Open it offline. See
[Snapshots](the-ui.md#snapshots).

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

The workload index advisor and the unused-index list. Parameters: `window` (default `24h`),
`db` (the database alias, default `default`) and `refresh=1`. The result is cached for
30 seconds per window and alias; `refresh=1` recomputes it.

```json
{
  "db": "default",
  "vendor": "postgresql",
  "window": 86400,
  "candidates": [{
    "table": "shop_order", "columns": ["customer_id", "status"],
    "create_sql": "CREATE INDEX shop_order_customer_id_status_idx ON shop_order (customer_id, status);",
    "covers": ["8a3c0f21d4b6e7a9"], "queries": 1840, "total_ms": 5120.4,
    "est_saving_ms": 4300.0, "vendor": "postgresql", "kind": "btree",
    "confidence": "high", "verified": true, "explained": true, "note": null
  }],
  "unused": [{"schema": "public", "table": "shop_order", "index": "shop_order_legacy_idx",
              "scans": 0, "size_bytes": 8388608,
              "drop_sql": "DROP INDEX \"public\".\"shop_order_legacy_idx\";"}],
  "unused_supported": true,
  "unused_error": null,
  "verified": true,
  "queries_analyzed": 62,
  "already_indexed": 9,
  "explained": 20,
  "elapsed_ms": 412.7,
  "cached": false
}
```

`candidates` is at most 10, best first. `kind` is `btree` or a text-search alternative
(`pg_trgm`, FTS5 or `FULLTEXT`; those have `confidence: "low"` and a `note`). `verified`
says the existing indexes were read from the database catalog, `already_indexed` how many
requirements an existing index already served, `explained` how many queries were checked
with a plain `EXPLAIN`. `unused` comes from PostgreSQL's `pg_stat_user_indexes` or SQL
Server's `sys.dm_db_index_usage_stats` (`unused_supported` is `false` for SQLite and
MySQL); `unused_error` is `{code, message}` when it could not be read. Counters reflect
the period since the statistics were last reset (SQL Server resets on restart). Nothing is ever
executed besides catalog `SELECT`s and plain `EXPLAIN`s. Answers `501 not_supported` if the
store cannot provide the workload. See [Database](database.md#index-advisor).

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

Runs started from the UI go through the same validation as a scenario file. A run's
`config` has secret-looking header values, body keys and URL secrets masked.

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

```bash
curl -s -X POST localhost:8000/_redline/api/runs -H 'content-type: application/json' -d '{
  "name": "smoke",
  "requests": [{"method": "GET", "path": "/api/orders/", "weight": 3}],
  "profile": {"mode": "steps", "start_rps": 10, "end_rps": 50, "step_rps": 10, "step_duration_s": 20}
}'
# 202 {"id": "7c1e...", "name": "smoke", "status": "running"}
```

It is refused with `403 load_disabled` unless `load_from_ui` is on (`403 read_only` in
read-only mode, `404 feature_disabled` when the `load` feature is off). The body needs a
`name`, a non-empty `requests` list and a `profile` with `mode` (`constant`, `ramp` or
`steps`), a positive `duration_s` and the rates for that mode. Every `path` must start with
a single `/`: a scheme or host is `400 invalid_body`. A `base_url` or `workers` key in the
body is ignored.

**The target** is the `base_url` setting when set. Otherwise it is the URL the request was
addressed to, accepted only when its host is a loopback name or one in `allowed_hosts`
(and carries no credentials); for any other host the call answers `400 invalid_body` and
asks you to set `base_url`. A `Host` header can no longer aim a run at another machine.
This is why a UI served at `https://staging.example.com` needs `base_url`, and why
`TestClient`-style hosts such as `http://testserver` are refused (FastAPI and Starlette's
`TestClient` default `base_url` is `http://testserver`).

**Limits** for runs started here: at most one hour, 5 000 rps and 200 steps. `status` goes
`running`, then `done`, `stopped` or `failed`. See [Load testing](load-testing.md#starting-a-run-from-the-ui)
and [Security](security.md#load-from-the-ui).

### Checks

[Refactor check](refactor-check.md): suites, runs and reports. All routes are under the
`checks` [feature switch](access-control.md#feature-switches) (`404 feature_disabled` when off), and every
`POST`, `PUT` and `DELETE` is refused with `403 read_only` in read-only mode. They need a
file-backed SQLite store (`501 not_supported` otherwise).

| Request | Returns |
|---|---|
| `GET api/checks/suites` | `{"items": [SuiteSummary]}` |
| `POST api/checks/suites` body `{"name", "suite"}` | `201` a `Suite` |
| `GET api/checks/suites/{id}` | a `Suite` |
| `PUT api/checks/suites/{id}` body `{"name", "suite"}` | `200` the updated `Suite` |
| `DELETE api/checks/suites/{id}` | `{"deleted": ...}`, with its runs. `409 run_active` while one is running |
| `GET api/checks/suites/{id}/export` | the suite as TOML, `text/plain`, `Content-Disposition: attachment; filename="<name>.toml"` |
| `POST api/checks/suites/draft` body `{"routes"?, "limit"?}` | a suite object built from seen traffic; not saved |
| `POST api/checks/runs` body `{"suite_id", "kind", "baseline_id"?, "label"?}` | `202 {"id", "status": "running", "kind", "suite_id", "baseline_id"}` |
| `GET api/checks/runs?suite_id=&kind=&limit=` | `{"items": [Run]}`, newest first. `kind` is `baseline` or `compare`; `limit` is 1 to 500 (default 50); a bad value is `400 invalid_filter` |
| `GET api/checks/runs/{id}` | a `Run` plus `suite`, `report` and `requests` |
| `POST api/checks/runs/{id}/stop` | `{"stopped": true}`; `false` when it was not running |
| `DELETE api/checks/runs/{id}` | `{"deleted": ...}`. `409 run_active` while running |
| `POST api/checks/baselines/import[?suite_id=]` | `201 {"id", "kind": "baseline", "requests"}` |

**Suites.** `suite` is the [suite file](refactor-check.md#the-suite-file) as an object (the same
keys as the TOML). It is validated by the same code as the command line: a bad suite, or any
method but `GET`, `HEAD` and `OPTIONS`, is `400 invalid_suite` with a message that names the
key. Bodies are limited to 1 MiB (`413 payload_too_large`). A `Suite` is

```json
{"id": "9b2e...", "name": "orders refactor", "requests": 3,
 "suite": {"name": "orders refactor", "base_url": "http://127.0.0.1:8000", "repeat": 20,
           "headers": {"Authorization": "Bearer ${env:API_TOKEN}"},
           "requests": [{"name": "list orders", "method": "GET", "path": "/orders?page=1"}]},
 "created_at": 1761500000, "updated_at": 1761500000}
```

Secret header values are returned as `[redacted]` unless they are only an `${env:NAME}`
placeholder (optionally after a scheme such as `Bearer`); a `PUT` that sends `[redacted]`
back keeps the stored value. A `SuiteSummary` is `{id, name, requests, created_at, updated_at,
last_baseline, last_compare}`, where the last two are `null` or `{id, status, label, git,
created_at, summary}`.

**Draft.** `routes` is a list of routes as `GET api/endpoints` shows them (`"/orders/{id}"`),
`"GET /x"` strings or `{"method", "route"}` objects; omit it for every read-only route.
`limit` is at least 1 (default 50; a larger value is capped at 200, and `0` or a non-number is
`400 invalid_body`). It uses the last 7 days of traffic and returns a suite object ready to edit; its only headers are the placeholders
`${env:REDLINE_CHECK_AUTHORIZATION}` and `${env:REDLINE_CHECK_COOKIE}`, and only when recent
requests carried those headers.

**Starting a run.** `kind` is `baseline` or `compare`. For a compare, `baseline_id` defaults to
the suite's newest finished baseline. The run is a background process; poll
`GET api/checks/runs/{id}` (the UI does, every second). The checks run in this order and the
first that fails answers: `403 checks_from_ui_disabled` (setting off, or `auto` outside debug
mode), `400 invalid_body`, `404 suite_not_found`, for a compare `400` (no baseline yet, or not
a finished baseline) and `404 check_run_not_found`, `409 run_active` (one run per suite at a
time), `501 not_supported` (no file-backed SQLite store), `400` when no target can be
determined (the same rule as [load runs](#runs): the `base_url` setting, else the server's own
port, never `Host`), and `400 invalid_suite` over the caps of 200 requests and 5 000 calls
(each request's warmup plus repeat, added up). A run is stopped after 15 minutes.

A `Run`:

```json
{
  "id": "5d0c...", "suite_id": "9b2e...", "name": "orders refactor", "kind": "compare",
  "baseline_id": "c41a...", "status": "done", "label": "after eager loading",
  "git": {"sha": "3f9a2c1e...", "branch": "refactor/orders", "dirty": false},
  "created_at": 1761500300, "finished_at": 1761500341,
  "summary": {"requests": 3, "faster": 1, "slower": 1, "changed": 1, "broken": 0, "passed": 1,
              "speed_change_pct": -11.4, "queries_before": 37, "queries_after": 12},
  "error": null, "progress": null, "request_count": 3,
  "base_url": "http://127.0.0.1:8000", "release": "3f9a2c1", "source": "ui",
  "baseline": {"id": "c41a...", "label": "before", "git": {}, "created_at": 1761500000}
}
```

`status` is `running`, `done`, `failed` or `stopped`; times are epoch seconds. `summary` is the
report's summary: besides the keys shown above it has `requests`, `perf`, `response`, `passed`,
`failed`, `failing`, `speed_ratio`, `queries_change`, `server_side`, `wins`, `regressions` and,
for a run that was stopped, `stopped_reason`. While running, `progress` is `{done, total, current, ...}`. `GET api/checks/runs/{id}` adds `suite` (redacted),
`report` and `requests`: for a **compare**, `report` is the full report in the same shape as
`redline check compare --json` (`{format: "redline-check-report", version, suite, baseline,
run, summary, requests: [...]}`, each request with `perf`, `server` and `response`) and
`requests` is `null`; for a **baseline**, `requests` is the per-request baseline data and
`report` is `null`.

**Import.** `POST api/checks/baselines/import` takes the JSON written by `redline check
record` (up to 10 MiB, else `413 payload_too_large`), so a baseline made in CI can be viewed
and compared against in the UI. `?suite_id=` attaches it to a saved suite. A document that is
not a `redline-check-baseline` of version 1, or that does not have the right shape, is
`400 invalid_baseline`. Suite headers and URLs, request paths, response headers and bodies are
redacted again on import.

### POST api/login, POST api/logout, GET api/config

Part of [access control](access-control.md#login-and-api).

| Request | Returns |
|---|---|
| `POST api/login` body `{"token": "..."}` or `{"username": "...", "password": "..."}` (4 KiB at most) | `200 {"ok": true, "expires_at": <epoch>}` and a session cookie (HttpOnly, SameSite=Strict, scoped to the mount, `Secure` on https). `400 invalid_json` / `invalid_body`, `401 invalid_credentials`, `429 too_many_requests`; `404 not_found` under a policy without login. It never redirects |
| `POST api/logout` | `200 {"ok": true}` and an expiring cookie, under any policy |
| `GET api/config` | `{preset, file, settings, sources, groups, warnings}`: the effective configuration, secrets as `"***"`, where each value came from (`default`, `adapter`, `preset`, `file`, `env`, `framework`), the settings grouped for display, and a list of warnings (configuration plus runtime notes of the access gate) |

`GET api/meta` also reports `features` (`explain`, `profile`, `replay`, `snapshot`, `load`,
`checks`, `logs`, `locals`, `sse`, and `load_from_ui` and `checks_from_ui`, which are the
setting and the feature and not `read_only`) and `access: {policy, login, read_only, preset}`, where `login` is
`"token"`, `"basic"` or `null`. The UI uses them to hide what is switched off.

## Things to know

- **The API is not versioned.** The development release is `0.5.0a1`: the shape may change between alpha
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


## Analysis tools

All routes are relative to the Redline mount and use its normal authentication and
read-only gate. See [Performance workflows](performance-workflows.md) for semantics.

| Method and route | Input / result |
|---|---|
| `GET api/analysis/health` | Latest retained trace sample, drop/profiling indicators and links to test requests |
| `GET api/analysis/coverage?suite_id=...` | Coverage of a saved suite against retained traffic |
| `POST api/analysis/source` | `{source, file?}` or `{project: true}`; findings and parse/read errors |
| `POST api/analysis/trace` | `{trace_id, settings: {...}}`; preview findings, without changing stored data or global settings |
| `POST api/analysis/database` | `{trace_id, span_id}`; on-demand catalog candidates and limitations |
| `POST api/analysis/demo` | `{}`; IDs of nine explicitly synthetic detector traces |
| `GET api/suppressions` | Persistent acknowledgement rules; raw CI findings are unaffected |
| `POST api/suppressions` | `{kind, reason, route?, fingerprint?, location?, expires_at?}`; created rule |
| `DELETE api/suppressions/{id}` | Remove an acknowledgement; traces remain intact |

Source scans and detector samples require `analysis_from_ui`; auto follows framework debug
mode. Coverage requires the checks feature, and database inspection requires the explain
feature and an adapter executor. Preview overrides accept only the detector thresholds
listed in Performance workflows. Synthetic samples cannot inspect a real database.

Invalid inputs use `invalid_source`, `invalid_thresholds`, `invalid_database_check` or
`invalid_suppression` (400). Other relevant codes include `analysis_disabled` (403),
`feature_disabled`, `trace_not_found`, `span_not_found`, `suite_not_found` (404),
`payload_too_large` (413), `explain_unavailable` and `not_supported` (501).

Check-suite request objects additionally accept `budgets`, `array_keys`, and `tolerances`;
suites accept `environment` labels. These fields survive save, retrieval and TOML export.
Reports add per-request budget results and applied comparison rules, and a `budget_failed`
summary count. Issue groups add `fingerprint`, `observed_total_ms`, `suppressed_routes` and
`suppressed`. Trace detail adds `runtime` and overlap-aware `diagnostics`.


## v0.4 SQL, job and privacy surfaces

### SQL and redaction analysis

Both routes require normal authentication, `analysis_from_ui` (or its debug-mode auto
resolution), and permission for POST under `read_only`. They accept bounded JSON input;
no DSN, arbitrary filesystem root or database execution request is accepted.

| Method and route | Request | Result |
|---|---|---|
| `POST api/analysis/sql` | `{ "source": "Python source", "dialect": "mssql" }`; dialect is optional (`other`) | `{findings, skipped, statements, note}`. Each finding has stable ID, rule kind, file/line, detail, confidence and SQL fingerprint. Dynamic/unreadable input is explicit, not a successful schema check |
| `POST api/analysis/redaction` | `{ "sample": {"password":"invented-value"}, "method":"POST", "route":"/private/" }` | `{sample, saved:false, note}` using current key/profile/route rules. It previews redaction, not whether capture is enabled, and does not persist a trace |

Malformed/unsupported input is `400 invalid_analysis`, oversized input is
`413 payload_too_large`, and a disabled tool is `403 analysis_disabled`. The existing
read-only gate takes precedence and returns `403 read_only`. A redaction sample is still
sent to this instance: use invented values, not actual secrets.

### Failed statements and connection evidence

A SQL span can add these fields to `data`:

```json
{
  "params_captured": false,
  "read_query": true,
  "rowcount_raw": -1,
  "statement_kinds": ["SELECT"],
  "comment_placeholders": 0,
  "error": {
    "type": "ProgrammingError",
    "sqlstate": "42S22",
    "native": 207,
    "category": "schema",
    "is_disconnect": false,
    "message": "[not captured]"
  }
}
```

Fields are additive and evidence-dependent. Success spans have no `error`. A failed
statement is not explainable. `rowcount_raw=-1` means unavailable, not zero rows or proof
of NOCOUNT. `connection` may include `age_s`, `statement_number`, pool settings and
bounded per-process hourly observations; absent values are unknown. A pool invalidation
can be a `pool_wait` span with `connection_error` instead of an invented SQL execution.

New issue kinds are `sql_error`, `application_error`, `placeholder_in_comment`,
`dml_rowcount_unknown`, `session_state_leak`, `fresh_connection_failure`,
`connection_churn` and `connection_failure`. Existing issue groups/history use their stable
IDs. A handled SQL error can belong to a status-200 recording. `tags.sql_errors` preserves
observed error counts when span evidence is truncated; clients must keep the incomplete
warning visible rather than interpreting absent spans as no errors.

`POST api/explain` supports SQL Server only through an executor with the appropriate
session-safe plan method. Optional `statistics` contains actual-plan logical-read summaries
when returned. Estimated plans do not execute the application query; actual plans require
the execution opt-in and are refused in read-only mode. SQL Server actual plans require a
sync driver with multiple result sets. Unsupported modes/permissions/parsing return the
structured EXPLAIN error rather than certifying success.

### Jobs and correlation

Jobs use existing trace endpoints, with `method = "TASK"` and tags such as `job.name`,
`job.id`, `job.queue`, `job.attempt`, `job.outcome`, `parent_trace_id` and `request_id`:

```text
GET api/traces?method=TASK&limit=100
GET api/traces?tag=job.id:example-task
GET api/traces?tag=request_id:example-request
GET api/jobs/summary?window=1d&limit=200
```

`job.outcome` distinguishes success, failure and retry observations. Stage spans use the
existing `custom` kind with `job_stage` metadata; the first stage that raises is tagged as
`job.first_failing_stage`. The summary endpoint aggregates retained task recordings (at
most 500, newest first) into outcome, latency and first-failing-stage counts without
reading span payloads. Row outcomes are bounded counters by reason digest, not captured
rows/IDs. `commit_observed_before_failure` is evidence that
an observed commit preceded a later failure, not proof that the whole job completed.
`user_id` is an explicitly resolved identifier; request IDs are labels, not authorization.

### Secondary stores and recording health

Secondary files participate in trace/job list, get and profile lookup only. Their traces
carry a `secondary_store` provenance tag. History/aggregates/live-tail/mutations remain
primary-only. EXPLAIN and catalog inspection on a secondary recording return
`403 secondary_store` because this UI cannot verify the worker's database binding.

`GET api/analysis/health` adds configured capture options and secondary-store count in
`capture`. Configuration is not proof that every worker or engine has been observed.
Keep the retained-sample and drop warnings visible. See [Production](production.md) for
separate-file deployment and validation requirements.


### Capture budgets and incomplete evidence

Effective configuration includes `max_sql_bytes`, `max_span_data_bytes`, `max_tag_bytes`
and `max_trace_bytes`. These limit captured SQL, span data, tag values and the completed
serialized trace checked before saving. They do not cap application payloads or total
process memory. Budget truncation/drop indicators must remain visible in API clients;
missing evidence is not a successful analysis result. Preserve reserved SQL/connection
failure counters when displaying shortened recordings. See [Settings](settings.md#capture-byte-budgets)
for defaults and [Production](production.md#sampling) for capacity validation.

A shortened recording can carry additive markers such as:

```json
{
  "truncated": {
    "capture_limits": true,
    "incomplete_evidence": true,
    "reason": "trace_budget",
    "budget_dropped_spans": 12
  }
}
```

Not every marker appears on every shortened recording. Existing `dropped_spans`/`max_spans`
count-cap fields can coexist with capture-budget fields. Changed SQL evidence has
`data.capture_incomplete = true`, cannot be explained, and withholds parameters.
`logs_dropped` includes omitted log records. Complete-evidence pytest/check assertions
must reject these recordings rather than passing on missing spans.

A privacy-processing failure can return a fresh minimal recording with
`path = "/[capture-unavailable]"` and `truncated.reason = "privacy_failure"`.
Application request content, source, user labels, error messages and arbitrary tags are
absent; observed failure counters can appear as `truncated.sql_errors` and
`truncated.connection_errors` instead of tags. `capture_failure`/`invalid_capture`
identify other safe fallback paths. A capture that cannot fit a valid minimal payload
is refused rather than saved as complete evidence.
