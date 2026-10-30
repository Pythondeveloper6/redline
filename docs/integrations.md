# Integrations

[← Index](index.md)

Redline records what your framework adapter hooks automatically: the ORM, templates, the
cache framework in Django, and so on. Everything else your request does, calling another
service, talking to Redis, querying PostgreSQL without an ORM or dumping data through a
serializer, is covered by **contrib integrations** in `redline_core.contrib`.

They share a few rules:

- **Opt-in.** Nothing is patched until you call the `instrument_*` function. They are
  never imported by `import redline_core`, so unused ones cost nothing.
- **Idempotent.** Call each once at startup (twice is harmless). Each has an
  `uninstrument_*` twin that restores the original.
- **Free outside a request.** Outside a traced request each wrapper is one
  `ContextVar.get()`.
- **Redacted.** URLs, keys and parameters pass through the same
  [redaction](security.md#redaction) as everything else.

| Library | Call | Records | Extra |
|---|---|---|---|
| SQLAlchemy | `instrument_engine(engine)` | `sql`, `transaction`, `pool_wait` spans | `sqlalchemy` |
| httpx | `instrument_httpx()` | `http_out` spans | `httpx` |
| requests | `instrument_requests()` | `http_out` spans | `requests` |
| boto3 | `instrument_boto3()` | `http_out` spans per SDK operation | `boto3` |
| redis-py | `instrument_redis()` | `cache` spans | `redis` |
| asyncpg | `instrument_asyncpg()` | `sql` spans | `asyncpg` |
| psycopg 3 | `instrument_psycopg()` | `sql` spans | `psycopg` |
| Marshmallow | `instrument_marshmallow()` | `serializer` spans | none |
| Strawberry | `RedlineExtension` | `resolver` spans | `strawberry` |
| Graphene | `RedlineMiddleware` | `resolver` spans | `graphene` |

The extras install the third-party library at a compatible version, for example
`pip install "redline-core[redis] @ git+https://github.com/Pythondeveloper6/redline#subdirectory=packages/redline-core"`.
The adapters re-export `instrument_httpx` (and `instrument_requests` for Flask) so older
imports keep working.

## SQLAlchemy

```python
from redline_core.contrib.sqlalchemy import instrument_engine

instrument_engine(engine)            # sync or async engine
```

The FastAPI, Flask and Litestar adapters call this for the engines you pass them. Use it
directly when you build an engine Redline does not know about. Beyond statement, parameters,
duration and callsite it records:

- **`transaction` spans** from the begin, commit and rollback events, with the outcome, the
  number of queries inside and the number of outbound HTTP calls made while the
  transaction was open. Empty transactions and control statements such as `SAVEPOINT` are
  not recorded. See [Transactions and locks](transactions.md).
- **`pool_wait` spans** when checking a connection out of the pool takes 1 ms or more,
  with the pool size and how many connections were already checked out.
- The SQLAlchemy [fix suggester](n-plus-one.md) and the EXPLAIN executor
  (`SQLAlchemyFixSuggester`, `explain_executor_factory`).

## httpx

```python
from redline_core.contrib.httpx import instrument_httpx

instrument_httpx()      # every httpx.Client / AsyncClient call becomes an http_out span
```

A span records method, URL (redacted), status and duration. The span name is
`GET host/path`. This feeds the [External calls](external-calls.md) page and the
`slow_external`, `duplicate_external` and `retry_storm` detectors.

## requests

```python
from redline_core.contrib.requests import instrument_requests

instrument_requests()   # patches requests.Session.send
```

Same data as httpx. Because `requests` is synchronous, calling it inside an `async def`
handler also blocks the event loop; see [Blocking in async](async-blocking.md).

## boto3

```python
from redline_core.contrib.boto3 import instrument_boto3

instrument_boto3()
```

Records one `http_out` span for each botocore client operation. Span names and synthetic URLs
contain only the service and operation (for example, `AWS s3 GetObject` and
`https://s3.amazonaws.com/__sdk__/GetObject`). Status and duration are recorded; API arguments,
response data, bucket and object keys, credentials, presigned URLs, and error messages are not
captured. If HTTP-level instrumentation also observes the SDK's network request, both spans may
appear: the SDK span measures the whole operation, while the HTTP span measures its network call.
`instrument_boto3()` returns `False` when botocore is unavailable.

## Redis

```python
from redline_core.contrib.redis import instrument_redis

instrument_redis()
```

Wraps `Redis.execute_command` for both the sync client and `redis.asyncio`, and
`Pipeline.execute`. Each command becomes a `cache` span with `op` (the command), `key`
(capped at 200 characters), `backend` (`redis`) and, for read commands, `hit` (`GET`, `GETEX`, `GETDEL`,
`HGET`, `MGET`, `HMGET`, `LINDEX`: a `None` result is a miss). A pipeline is **one** span
(`op="pipeline"`); the commands queued on it are not recorded individually, and spans
without a key are not included in the [cache statistics](cache-efficiency.md).

Django's cache framework (including a Redis cache backend) is already recorded by
`django-redline`; use this when you talk to Redis directly, from Flask, FastAPI or
Litestar, or from Django code that bypasses `django.core.cache`.

## asyncpg

```python
from redline_core.contrib.asyncpg import instrument_asyncpg

instrument_asyncpg()
```

For apps that use asyncpg without an ORM. Wraps `Connection.execute`, `fetch`, `fetchrow`,
`fetchval` and `executemany`; pool methods delegate to these, so pooled connections are
covered. Statements are recorded in asyncpg's `$1` placeholder style. Row counts are filled
in where the result exposes them, which enables the
[large-result detectors](result-size.md).

## psycopg 3

```python
from redline_core.contrib.psycopg import instrument_psycopg

instrument_psycopg()
```

For psycopg 3 used directly, sync or async cursors. The patch is process-wide (a
connection argument is accepted and ignored). If you use SQLAlchemy with the psycopg
driver you do **not** need this: SQLAlchemy's events already see those queries, and
patching both would record each query twice.

!!! warning "One path per query"

    Use exactly one capture path per database driver: the ORM's (Django, SQLAlchemy) *or*
    the driver-level one (asyncpg, psycopg). If you instrument SQLAlchemy and asyncpg and
    use SQLAlchemy's asyncpg dialect, every query is recorded twice.

## Marshmallow

```python
from redline_core.contrib.marshmallow import instrument_marshmallow

instrument_marshmallow()
```

Patches `Schema.dump`. Only the **outermost** `dump` becomes a `serializer` span; nested
schemas (`fields.Nested`) are part of their parent's time. Feeds the
[slow serializer detector](serializers.md). Marshmallow has no extra: the call
returns `False` if marshmallow is not installed.

## GraphQL

Strawberry and Graphene resolvers are described on their own page:
[GraphQL](graphql.md).

## Writing your own

The building blocks are public. `redline_core.record_span` and `redline_core.record_query`
add a span to the current request from any code:

```python
import time
from redline_core import record_span

start = time.perf_counter()
result = my_client.call()
# record_span(kind, name, start, duration_in_seconds, data)
record_span("http_out", "POST payments.example/charge", start,
            time.perf_counter() - start, {"status": 200, "method": "POST"})
```

For plain timing of your own code, use `redline_core.span` ([Custom spans](custom-spans.md)).
For a full integration with its own fix suggestions see [Extending](extending.md).


## Checking integration coverage

Open **Analysis → Recording health** (or `GET api/analysis/health`) to distinguish whether
an optional provider is installed, loaded in this process and instrumented. The check
never imports providers or enables hooks. An inactive hook means those calls may be
missing; an active hook still does not prove calls occurred or cover other workers.
HTTPX, Redis and boto3 remain explicit opt-ins.

SDK operation spans use a `SDK:<operation>` method and a synthetic service URL. Because
resource identity and individual retry attempts are deliberately omitted, these spans
contribute latency/error/count totals but do not establish duplicate calls or retry
storms. This avoids treating reads of different object keys as identical requests.
