# External calls

[← Index](index.md)

Most backends spend more time waiting on other services than on their own database. The
payment gateway, the email API, the search cluster, a partner's REST endpoint: each is an
`http_out` span in the trace, and together they are a page of their own.

## Recording them

Outbound calls are recorded once you instrument the client you use
([Integrations](integrations.md)):

```python
from redline_core.contrib.httpx import instrument_httpx
from redline_core.contrib.requests import instrument_requests

instrument_httpx()
instrument_requests()
```

A span records method, URL (query-string secrets masked), status and duration. In the
Timeline it is purple and named `GET api.example.com/v2/users/42`.

## Detectors

Three detectors read `http_out` spans in one request:

| Kind | Fires when | Severity |
|---|---|---|
| `slow_external` | a call takes at least `slow_external_ms` (default 500), grouped by method, URL without query string, and callsite | `critical` at 5x |
| `duplicate_external` | the same method and **full URL** is called twice or more | `warning` at 5 copies, or when together they exceed `slow_external_ms` |
| `retry_storm` | the same method and **host** is called three or more times with at least one failure | `critical` at 6 calls |

A *failure* here means a 5xx, a 429, or no status at all (timeout, connection error). A
group that is already a retry storm is not reported again as a duplicate.

```text
Retry storm: 5 calls to api.mailer.example, 3 failed          (shop/notify.py:19)
Duplicate external call: GET api.rates.example/usd ×4         (shop/pricing.py:62)
Slow external call: POST api.payments.example/charge, 1.8 s   (shop/checkout.py:41)
```

Common fixes: cache the response for the duration of the request (a duplicate), cap the
retries and add jitter (a storm), set a short timeout and move the call off the request
path (slow), and never call out inside a transaction
([Transactions and locks](transactions.md#external-call-inside-a-transaction)).

## The External page

Across requests, **External calls** groups every outbound call by method, host and a **URL
pattern**: digit runs, UUIDs and hex strings of eight or more characters become `*`, so
`/v2/users/42` and `/v2/users/77` are one row, `/v2/users/*`.

| Column | Meaning |
|---|---|
| Method, Host, URL pattern | the group |
| Calls | total calls in the window |
| p95, max | latency. p95 is taken over each request's *mean* for the pattern (exact when a request calls it once) |
| Errors, error rate | 5xx, 429 and status-less calls |
| Duplicates | requests that called this pattern two or more times |
| Retries | requests with three or more calls and at least one error |

The window picker (1 h, 24 h, 7 d) and the sort let you find the slowest dependency first.
The same data is served by `GET api/external?window=`
([HTTP API](http-api.md#get-apiexternal)). It is derived from the `trace_ext` table, so it
reflects *kept* traces: with sampling, counts are of the sample.

## Settings

| Setting | Default |
|---|---|
| `slow_external_ms` | `500` |
