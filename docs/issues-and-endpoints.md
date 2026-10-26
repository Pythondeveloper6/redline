# Issues and endpoints

[← Index](index.md)

The request list shows single requests. Two pages answer the questions you ask next:
**which routes are slow** (Endpoints) and **which problems keep happening** (Issues).
Both are aggregates over the traces in the store, so they share one caveat that is worth
reading before the details.

!!! warning "Aggregates describe what was stored, not all traffic"

    Endpoints and Issues are computed from **stored** traces. With `sample_rate = 1.0`
    (the development default) that is every request. In production you will sample, and
    you will also **always keep** slow requests and errors. Together that means stored
    traces over-represent the bad ones:

    - **Counts** are counts of stored traces, not requests served.
    - **Percentiles** are biased upward: the fast majority is thinned out while every slow
      request is kept. Treat p95 and p99 as an upper bound on a sample, not a measurement
      of your traffic.
    - **Error rate** is the share of *stored* requests that were errors, so it is
      inflated by the `always_keep_errors` safety net.

    For true rates and latency distributions in production use your metrics system or an
    APM. Use Redline to find *which* endpoint and *which query*.

## Endpoints

```text
GET api/endpoints?window=1h|24h|7d
```

One row per **(route, method)**. The route is the **template** (`/orders/{order_id}`,
`/orders/<int:pk>/`), so `/orders/1` and `/orders/2` are one row. A request with no
resolved route (a 404 for a path no route matches) is grouped by its concrete
**path**.

!!! warning "Unmatched paths multiply rows"

    Scanners and typos that match no route each become their own row, because there is no
    template to group them by. If you see hundreds of rows for paths like `/wp-login.php`,
    that is the cause. Add a prefix or `^` regex to `ignore_paths`, or filter them at the
    proxy.

| Column | Definition |
|---|---|
| Requests | stored traces in the window for this route and method |
| Errors | the share with **status >= 500**. A 4xx is not an error here |
| p50, p95, p99 | percentiles of `duration_ms`, linearly interpolated between the two nearest samples; `0` for an empty set |
| Avg queries, Max queries | mean and maximum of `query_count` |
| Avg query time | mean of `query_ms` (the API field `avg_query_ms`) |
| Issues | the **sum** of `issue_count` over those traces: the same issue seen in 40 requests counts 40 |
| p95 trend | a sparkline of p95 per time bucket |

### Windows and buckets

`window` accepts `1h`, `24h` (default) and `7d`, or any `<n>m`, `<n>h`, `<n>d`. It is
measured back from now. The window is divided into **48 equal buckets** (a 24 h window has
30-minute buckets). `series` lists only the **non-empty** buckets, each as
`{t, p95_ms, count}` with `t` the bucket start in epoch seconds. A route with a single
request therefore has a one-point series, and the sparkline shows `-`.

Rows are ordered by request count, then p95, then route name. The UI re-sorts as you click
headers.

### How to use it

| You see | It suggests |
|---|---|
| High p95, low average queries | time is not in SQL: look at the [flamegraph](flamegraphs.md) or outbound HTTP spans |
| High average queries | an [N+1](n-plus-one.md) or missing eager loading on this route |
| High p99 but modest p95 | a tail problem: lock contention, a cache miss path, GC; open the slowest requests from the list (filter by route) |
| Rising sparkline | a regression: look for what changed around the bucket where it started |
| Errors above 0 | open the 5xx traces: the traceback is stored |

## Issues

```text
GET api/issues?window=...&kind=n_plus_one|duplicate_query|slow_query|too_many_queries
```

Every kept trace's issues are stored in a table of their own, and the Issues page
**groups them by issue id**. The id is `sha1(kind | fingerprint | file:line)[:16]`, so "the
same problem at the same line" is one group however many requests hit it.

| Field | Meaning |
|---|---|
| `occurrences` | the number of stored traces that contained this issue in the window |
| `first_seen`, `last_seen` | epoch seconds of the earliest and latest trace |
| `routes` | every route (or path) the issue appeared on |
| `sample_trace_id`, `sample_span_ids` | the **latest** occurrence, which **Open sample request** links to |
| `title`, `detail`, `severity`, `suggestion`, `callsite` | taken from the **latest** occurrence |

Groups are ordered by severity (critical first), then by occurrences, then by recency.

### Behaviour worth knowing

- **The counts in a title are from the latest sample.** "N+1: 30 queries" reflects the
  most recent request. A list that grew since will say a bigger number. Use
  `occurrences` for "how often" and open the sample for "how bad".
- **Moving a line makes a new issue.** The line number is part of the id, so editing a
  file above the offending line splits one issue into two (the old one fades out as
  retention removes its traces). Fix the cause rather than watching the counter.
- **`too_many_queries` is per endpoint**, not per line: its location is
  `"<METHOD> <route or path>"`.
- **One shared line is one issue.** The id uses the innermost project frame, so an N+1 in
  a helper called by two views is a single issue with both routes in `routes`.
- **Nothing is ever "resolved".** An issue disappears from the list when its stored
  traces age out of the window or retention. After you deploy a fix, re-exercise the
  endpoint and watch `last_seen` stop advancing.

### Filtering

| Filter | Effect |
|---|---|
| Kind | one of the four issue kinds. Any other value returns `400 invalid_kind` |
| Window | `1h`, `24h`, `7d` (or `<n>m|h|d`). A bad value returns `400 invalid_window` |

## Using both together

A good loop is:

1. **Endpoints**, sorted by p95: find the worst route.
2. **Requests**, filter by that route (the API supports `route=`) and `min_ms`: open the
   slowest.
3. **Issues** tab of that request: the fix, with confidence.
4. Apply it, re-exercise, and watch the route's p95 and **Issues** count drop.
