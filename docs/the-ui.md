# The UI

[← Index](index.md)

The web UI is a React single-page app, built ahead of time and shipped inside
`redline-core`. Your adapter serves it from your own application (`/redline/` for
Django, `/_redline/` for FastAPI), so there is nothing to host and no Node.js to install.

It is a client of the [HTTP API](http-api.md), using relative URLs (`./api/...`) and hash
routing (`#/requests`, `#/requests/<id>`, `#/endpoints`, `#/issues`). That is why it works
under any mount prefix and needs no catch-all route. Because it follows the system
colour scheme and has a theme switch, every screenshot below is shown in the theme you are
reading these docs in.

## The top bar

![Request detail page with the top bar](assets/screenshots/django-n-plus-one-light.png){ .rl-shot .rl-only-light }
![Request detail page with the top bar](assets/screenshots/django-n-plus-one-dark.png){ .rl-shot .rl-only-dark }

| Control | What it does |
|---|---|
| **Requests**, **Endpoints**, **Issues** | The three top-level pages |
| Framework and version label | `django v0.1.0a1` or `fastapi v0.1.0a1`; hover for the storage path |
| Theme switch | Light, system or dark. Remembered in the browser's `localStorage` (`redline-theme`) |
| **Clear all** | Deletes every stored request, query, profile and issue, after a confirmation dialog. It calls `DELETE api/traces` and cannot be undone |

## Requests

The live list of recorded requests, newest first.

![Requests page](assets/screenshots/fastapi-light-requests.png){ .rl-shot .rl-only-light }
![Requests page](assets/screenshots/fastapi-dark-requests.png){ .rl-shot .rl-only-dark }

Each row shows the method, the concrete path with the route template beneath it (when
they differ), the status, the duration with a bar, the query count, the time in queries,
badges for any issue kinds found, and how long ago it happened. The duration bar is shaded by how much of the request was spent in
queries, and turns amber at 100 ms and red at 500 ms.

### Filters

| Filter | Options | API parameter |
|---|---|---|
| Search | text matched against path, route or view | `q` |
| Method | any, GET, POST, PUT, PATCH, DELETE | `method` |
| Status | any, 2xx, 3xx, 4xx, 5xx | `status` |
| Duration | any, 50 ms, 100 ms, 500 ms, 1 s or more | `min_ms` |
| Queries | any, 5, 10, 25, 50 or more | `min_queries` |
| Has issues | checkbox | `has_issues=1` |

The API accepts more than the UI offers (an exact status code, `route`, `tag=key:value`):
see [The HTTP API](http-api.md#get-apitraces). **Reset** clears all filters.

### Live polling

By default the list polls every two seconds and shows a pulsing **Live** button. Click it to
**Pause** (the button becomes **Resume**) while you read. The counter shows how many
rows are loaded of how many match. Older rows load with cursor pagination.

### Keyboard

| Key | Action |
|---|---|
| `/` | Focus the search box |
| `j` / `k` | Move the selection down / up |
| `Enter` | Open the selected request |
| `Esc` | Leave the search box; close a drawer or dialog |

### The empty state

With nothing recorded yet the page shows a ready-to-copy installation snippet for your
framework, which is a reasonable thing to see on a fresh install.

## Request detail

Click a request. The header shows the method, path, status, start time, framework, route
and view. If the request raised, a red box shows the exception type, message and a
collapsible **traceback**. Under it five cards:

| Card | Shows |
|---|---|
| Status | the status code and the response size |
| Duration | wall time and the number of spans |
| Queries | the query count (amber from 50) and the average per query |
| Query time | time in SQL and its share of the request |
| Issues | the number of issues and their kinds, or "clean" |

Then five tabs. The selected tab is kept in the URL (`?tab=sql`).

### Timeline

![Timeline of a request](assets/screenshots/fastapi-light-detail-timeline.png){ .rl-shot .rl-only-light }
![Timeline of a request](assets/screenshots/fastapi-dark-detail-timeline.png){ .rl-shot .rl-only-dark }

A waterfall of the span tree: each row is a span, indented by depth, with a bar placed
on the request's time axis.

- Span kinds are colour-coded: `sql` blue, `cache` amber, `http_out` purple, `template`
  teal, `middleware` grey, `view` green, `custom` pink.
- The chips above the chart **toggle a kind** on and off and show its count. Hiding
  `middleware` and `view` is a quick way to see only the I/O.
- A triangle collapses a span's children.
- A red dot marks spans that belong to an issue.
- Hover for a tooltip with the kind, duration, start offset and callsite; click to open
  the span drawer.

### The span drawer

Clicking a span opens a drawer from the side. For SQL it shows the formatted statement,
**Copy SQL**, an **Explain** button (enabled only when the query is explainable), the
parameters, the database alias and vendor, the row count and the fingerprint. For other
kinds it shows their data: cache operation, key and hit; the outbound request and status;
the template name. Below, the **call site**: file, line, function and source line, and the
project stack, outermost first.

### SQL

Queries grouped by fingerprint, sorted by total time. Each group shows the normalised
statement, the callsite, an issue badge if it belongs to one, the total time, the number
of executions and the slowest. Expand a group to see the full statement and a table of
every execution (the first 50) with start, duration, parameters and rows, each with its
own **Explain** button.

### Issues

![Issues tab with an N+1 and its diff](assets/screenshots/django-n-plus-one-light.png){ .rl-shot .rl-only-light }
![Issues tab with an N+1 and its diff](assets/screenshots/django-n-plus-one-dark.png){ .rl-shot .rl-only-dark }

One card per issue: severity, kind, the number of occurrences and total time, a title,
a plain-English detail, the offending source line, and the **suggested fix**: a summary,
a one-line code hint, a unified diff with **Copy diff**, a link to the relevant
documentation, and a confidence pill (`high`, `medium` or `low`).
See [N+1 detection](n-plus-one.md).

### Profile

The flamegraph, or an explanation of how to get one. See [Flamegraphs](flamegraphs.md).

### Request / Response

![Redacted request](assets/screenshots/django-redaction-light.png){ .rl-shot .rl-only-light }
![Redacted request](assets/screenshots/django-redaction-dark.png){ .rl-shot .rl-only-dark }

Trace tags as pills, then request headers, query parameters, the request body, response
headers and the response body. Masked values are shown as a distinct **redacted**
marker, a **truncated** pill flags a body cut at `max_body_bytes`, and JSON bodies are
pretty-printed with a copy button. See [Security](security.md#redaction).

## The EXPLAIN drawer

![EXPLAIN drawer](assets/screenshots/fastapi-light-explain.png){ .rl-shot .rl-only-light }
![EXPLAIN drawer](assets/screenshots/fastapi-dark-explain.png){ .rl-shot .rl-only-dark }

Opens when you click **Explain**, and immediately runs an estimate-only `EXPLAIN`. It
shows the plan as a tree (with cost and row estimates and flag chips), the findings,
the suggested `CREATE INDEX` statements with **Copy SQL**, and a **Raw plan** toggle.
**Run with ANALYZE** is always visible; if `explain_analyze` is off, the drawer explains
that instead of running it. See [EXPLAIN](explain.md).

## Endpoints

![Endpoints page](assets/screenshots/fastapi-light-endpoints.png){ .rl-shot .rl-only-light }
![Endpoints page](assets/screenshots/fastapi-dark-endpoints.png){ .rl-shot .rl-only-dark }

A table of every route and method seen in the window (1 h, 24 h or 7 d): request count,
error rate, p50, p95, p99, average and maximum queries, issue count and a p95 trend
sparkline. Click a column header to sort. Percentiles and rates are defined in
[Issues and endpoints](issues-and-endpoints.md#endpoints).

## Issues

![Issues page](assets/screenshots/django-issues-light.png){ .rl-shot .rl-only-light }
![Issues page](assets/screenshots/django-issues-dark.png){ .rl-shot .rl-only-dark }

Problems deduplicated across requests: severity, kind, occurrences, first and last seen,
the routes affected, the callsite and the suggested fix. **Open sample request** jumps
to the latest occurrence. Filter by kind and window.

## On a phone

The layout is responsive: the navigation wraps under the logo, columns drop out of the
tables below breakpoints (p50, p99 and the query columns hide first).

## What the UI does not do

- It does not edit anything: the only mutating actions are **Clear all** and running an
  EXPLAIN.
- It cannot delete a single request (the API can: `DELETE api/traces/<id>`).
- It has no saved filters, no sharing links to filtered views, and no auth of its own: it
  trusts the adapter's [access control](security.md#who-can-open-the-ui).
