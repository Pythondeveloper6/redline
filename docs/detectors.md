# Detectors overview

[← Index](index.md)

Version 0.1 found four kinds of problem: N+1 queries, duplicate queries, slow queries and
requests with too many queries. Version 0.2 adds ten more, plus aggregate pages that look
across requests. This page is the map; each family has its own page.

## Every issue kind

An *issue* is something the detectors found in one trace. Issues with the same identity
(kind, query or call, and callsite) are deduplicated across requests on the
[Issues](issues-and-endpoints.md#issues) page, with first seen, last seen and a count.

| Kind | Fires when | Default threshold | Page |
|---|---|---|---|
| `n_plus_one` | the same read from the same line, at least N times | `n_plus_one_threshold = 5` | [N+1 detection](n-plus-one.md) |
| `duplicate_query` | identical SQL and parameters repeated | `duplicate_threshold = 2` | [N+1 detection](n-plus-one.md) |
| `slow_query` | a query takes at least this long | `slow_query_ms = 100` | [N+1 detection](n-plus-one.md) |
| `too_many_queries` | a request runs at least this many queries | `too_many_queries = 50` | [N+1 detection](n-plus-one.md) |
| `blocking_in_async` | the event loop was stalled by synchronous work | `blocking_ms = 20` | [Blocking in async](async-blocking.md) |
| `long_transaction` | a transaction stayed open this long | `long_transaction_ms = 200` | [Transactions and locks](transactions.md) |
| `external_in_transaction` | an outbound HTTP call was made inside a transaction | any | [Transactions and locks](transactions.md) |
| `large_result` | a SELECT returned this many rows | `large_result_rows = 1000` | [Large and unbounded results](result-size.md) |
| `unbounded_query` | a SELECT with no `LIMIT` returned this many rows | `unbounded_rows = 200` | [Large and unbounded results](result-size.md) |
| `inefficient_count` | `len(qs)`, `if qs:`, `bool(qs)` or `.count() > 0` on a full-row query | pattern | [Count and exists](result-size.md#count-and-exists) |
| `slow_serializer` | a serializer takes at least this long | `slow_serializer_ms = 50` | [Slow serializers](serializers.md) |
| `slow_external` | an outbound call takes at least this long | `slow_external_ms = 500` | [External calls](external-calls.md) |
| `duplicate_external` | the same method and URL called twice or more | 2 | [External calls](external-calls.md) |
| `retry_storm` | three or more calls to one host with at least one failure | 3 | [External calls](external-calls.md) |

Severity is `info`, `warning` or `critical`. For most of the new detectors, a value at five
times its threshold is `critical`.

## Where the data comes from

The detectors read **spans**, and the new ones need new span kinds. The adapters and the
[integrations](integrations.md) create them.

| Span kind | Created by | Used by |
|---|---|---|
| `sql` (with `rows`, `has_limit`, `in_transaction`) | ORM and driver hooks | result-size detectors, N+1 |
| `serializer` | DRF, FastAPI response serialization, Marshmallow | `slow_serializer` |
| `resolver` | Strawberry, Graphene | N+1 over [GraphQL](graphql.md) |
| `transaction` | Django, SQLAlchemy | transaction detectors |
| `blocking` | the event-loop lag monitor, sync-driver hooks | `blocking_in_async` |
| `pool_wait` | SQLAlchemy connection checkout | shown in the timeline |
| `http_out` | httpx, requests integrations | external detectors |
| `cache` | Django cache, redis-py | the Cache page |

## Aggregate pages

Detectors look at one request. Some questions are about many requests, so Redline derives
small per-request summary tables at write time and the UI queries those:

| Page | Question | Source table |
|---|---|---|
| [Database](database.md) | Which SQL costs the most in total? Which index is missing or unused? | `trace_sql` |
| [Cache](cache-efficiency.md) | Which key patterns miss, are written but never read, or stampede? | `trace_cache` |
| [External calls](external-calls.md) | Which third parties are slow, duplicated or retried? | `trace_ext` |
| [History](history.md) | How did p95 change per release? What is new, resolved or regressed? | `rollups`, `issue_index` |

## Logs and exceptions

Two pieces of context are not issues but make every issue easier to understand: the **log
records** emitted during the request, and the **local variables** of the project frames in
an exception. See [Logs and exceptions](logs-and-exceptions.md).

## Tuning

Thresholds are ordinary [settings](settings.md#detectors). A noisy detector is usually a
threshold that is too low for your data: raise `large_result_rows` for a reporting app, or
`slow_external_ms` for a service that calls a slow vendor on purpose. There is no switch to
turn a single detector off; the registry is code (see [Extending](extending.md)).

All detectors run when a trace is **finished**, and only on kept traces, so a request that
is sampled out pays nothing for them. One detector raising an exception is logged and
skipped; it never fails the request.
