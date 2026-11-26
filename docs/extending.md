# Extending

[← Index](index.md)

Redline has two extension points, both small `typing.Protocol`s in `redline_core`, and a
helper for making any other client visible:

| Extension | You write | Use it for |
|---|---|---|
| [`FixSuggester`](#fixsuggester) | one method, `suggest(issue, spans)` | fix advice for another ORM, a house style, or a framework Redline does not know |
| [`Store`](#the-store-protocol) | the storage interface | keeping traces somewhere other than the local SQLite file |
| [`instrument_client_method`](#instrumenting-a-client) | one call per client method | spans for a database, queue or storage client Redline has no integration for |

All three are **beta** surfaces: signatures may change before 1.0.

## The data you will touch

All of it is plain dataclasses in `redline_core.model` (and re-exported from
`redline_core`). The public surface and its stability rules are in [API stability](api-stability.md).

```python
@dataclass
class Span:
    id: str
    parent_id: str | None
    kind: str                       # sql, cache, http_out, template, middleware, view, custom
    name: str
    start_ms: float
    duration_ms: float
    callsite: Callsite | None
    data: dict                      # kind-specific; for sql: sql, params, fingerprint,
                                    # db_alias, vendor, rows, many, explainable

@dataclass(frozen=True)
class Callsite:
    file: str                       # relative to project_root when possible
    line: int
    function: str
    code: str | None                # the source line, stripped
    stack: tuple[Frame, ...]        # up to 12 project frames, innermost last
    # .location -> "file:line"

@dataclass
class Issue:
    id: str
    kind: str                       # n_plus_one, duplicate_query, slow_query, too_many_queries
    severity: str                   # info, warning, critical
    title: str
    detail: str
    span_ids: list[str]
    fingerprint: str | None
    callsite: Callsite | None
    count: int
    total_ms: float
    suggestion: Suggestion | None

@dataclass
class Suggestion:
    summary: str                    # "Add .prefetch_related('items') to the Order queryset"
    code: str | None                # a one-line fix
    diff: str | None                # a unified diff, when you could locate the code
    confidence: str                 # high, medium, low
    docs_url: str | None
```

## FixSuggester

```python
from typing import Protocol

class FixSuggester(Protocol):
    def suggest(self, issue: Issue, spans: list[Span]) -> Suggestion | None: ...
```

The detector calls each registered suggester **in order** for every issue. `spans` are the
issue's SQL spans (`issue.span_ids` resolved). Return a `Suggestion`, or `None` to pass.

- **The first non-`None` result wins.** Order therefore matters.
- The built-in `GenericSuggester` (confidence `low`) is always **last**, so you only need to
  handle the cases you can do better.
- **An exception is logged and skipped**: the next suggester runs. A broken suggester
  never breaks a request.
- Suggesters run for **every** issue kind. Return `None` for kinds you do not handle (the
  Django suggester handles `n_plus_one` and `duplicate_query`; the SQLAlchemy one only
  `n_plus_one`).
- They run on the **request path** when the trace finishes (for kept traces). Keep them
  fast; parse source files once and cache them.

### A worked example

A suggester for a Tortoise-ORM style `prefetch_related`, matching on table name:

```python
from redline_core import Issue, Span, Suggestion

class TortoiseSuggester:
    def suggest(self, issue: Issue, spans: list[Span]) -> Suggestion | None:
        if issue.kind != "n_plus_one" or issue.callsite is None:
            return None
        table = _table_of(spans[0].data["fingerprint"])      # your own helper
        relation = RELATIONS.get(table)                       # {"orderitem": "Order.items"}
        if relation is None:
            return None                                       # let the next suggester try
        owner, attr = relation.split(".")
        return Suggestion(
            summary=f"Prefetch {relation} on the {owner} query.",
            code=f'{owner}.all().prefetch_related("{attr}")',
            diff=None,                                        # no source location: hint only
            confidence="medium",
            docs_url="https://tortoise.github.io/query.html#prefetching",
        )
```

### Registering it

The detector lives on the recorder. Register after Redline is set up.

=== "FastAPI"

    ```python
    redline = install(app, engine=engine)
    redline.recorder.detector.add_suggester(TortoiseSuggester())
    ```

=== "Django"

    ```python
    # e.g. in an AppConfig.ready()
    from django_redline.runtime import get_runtime

    get_runtime().recorder.detector.add_suggester(TortoiseSuggester())
    ```

`add_suggester` inserts yours **ahead of the generic fallback but after the adapters' own
suggesters**, so the built-in Django or SQLAlchemy advice is tried first and yours only
sees what they declined.

!!! warning "Django: the runtime is rebuilt when `REDLINE` or `DEBUG` change"

    `django_redline.runtime` rebuilds the recorder on the `setting_changed` signal (this
    happens in tests with `override_settings`). A suggester registered on the old recorder
    does not carry over. Register again after a rebuild, or register from code that runs
    per runtime.

### Writing a diff

A diff is a plain unified diff against the file in the callsite. Build it with
`difflib.unified_diff` using `a/<path>` and `b/<path>` headers, as the built-in suggesters
do, so the UI's **Copy diff** produces something `git apply` accepts when run from the
project root. The file path in `Callsite.file` is relative to `project_root`.

Only produce a diff when you are **sure** you found the right expression. A wrong diff is
worse than a hint. Use `confidence` honestly: `high` means "relation and line identified".

## Instrumenting a client

Time spent in a client Redline cannot see shows up as [unattributed time](detectors.md#unattributed-time)
on the request. `instrument_client_method` makes it visible: it wraps one method of a client
class (or one object) and records every call as a span with the client name, the operation,
a target, the duration, a payload size and, on failure, the exception class.

```python
from redline_core import instrument_client_method
from redline_core.extending import payload_size

restore = instrument_client_method(
    acme_sdk.Client,                     # a class (all instances) or one object
    "put",
    client="acme",                       # span data["client"]
    operation="write",                   # a string, or a callable (self, args, kwargs) -> str
    target=lambda self, args, kwargs: args[0],                      # e.g. the table name
    payload_bytes=lambda self, args, kwargs: payload_size(args[1]),  # request size
    result_bytes=payload_size,           # response size, from the return value
)
# restore() puts the original method back
```

| Property | Behaviour |
|---|---|
| Span | kind `custom` (override with `kind=`), name `acme write orders`, data `client`, `op`, `target`, `payload_bytes`, `error`, and `result_bytes` when given |
| Sizes | `payload_size(value)` measures only bytes-like and text values; anything else is `None` rather than serialized to be measured |
| Errors | the exception class name only, never its message; the exception is re-raised unchanged |
| Safety | a hook that raises is ignored (the call still runs and is recorded); the target is redacted and capped at 200 characters |
| Async | coroutine functions are wrapped as coroutines |
| Idempotent | wrapping an already wrapped method does nothing; the undo function restores the original and does nothing if another patch replaced yours |
| Cost | outside a traced request, one `ContextVar` read per call |

Use it on instance methods; static and class methods are not supported. For code you cannot
patch, `client_span` is the context-manager form:

```python
from redline_core import client_span

with client_span("acme", "scan", target="orders") as info:
    rows = acme_client.scan("orders")
    info["payload_bytes"] = len(rows)
```

The first-party [pymongo, Kafka, pika, Google Cloud Storage and Azure Blob integrations](integrations.md#data-stores-queues-and-object-storage)
are built on this helper.

## The `Store` protocol

```python
class Store(Protocol):
    path: str

    def save(self, trace: Trace) -> None: ...
    def get(self, trace_id: str, *, include_profile: bool = True) -> Trace | None: ...
    def get_profile(self, trace_id: str) -> dict | None: ...
    def has_profile(self, trace_id: str) -> bool: ...
    def list(self, filters: Filters | None = None, limit: int = 50,
             cursor: str | None = None) -> Page: ...
    def endpoints(self, window: float) -> list[EndpointStats]: ...
    def issues(self, window: float, kind: str | None = None) -> list[IssueGroup]: ...
    def delete(self, trace_id: str) -> int: ...
    def clear(self) -> int: ...
    def prune(self, max_age_s: float | None = None, max_rows: int | None = None) -> int: ...
    def count(self) -> int: ...
    def flush(self, timeout: float | None = None) -> bool: ...
    def close(self) -> None: ...
```

### The contract

| Method | Obligations |
|---|---|
| `save` | **Must never block the request.** Enqueue and return; drop and count rather than wait. It is called on the request path, once per kept trace |
| `get` | Return the full trace, or `None`. `include_profile=False` must omit the (large) profile |
| `list` | Newest first. `filters` carries `q`, `method`, `status`, `min_ms`, `min_queries`, `has_issues`, `route`, `tag`. Return a `Page(items, next_cursor, total)` of `TraceSummary` dicts (`Trace.summary()`). Cursors must stay valid while new rows arrive |
| `endpoints` | One `EndpointStats` per `(route or path, method)` in the window; see [the definitions](issues-and-endpoints.md#endpoints). `series` has at most 48 non-empty buckets |
| `issues` | Issue groups deduplicated by `Issue.id`, the sample being the latest occurrence |
| `delete`, `clear`, `prune` | Return the number of traces deleted. `prune(None, None)` applies the store's own rules; `0` disables a rule for that call (never "delete all"); negative values raise `ValueError` |
| `flush` | Block until everything saved so far is durable; `False` on timeout. Called on shutdown |
| `close` | Flush and release resources. `save` after `close` should drop |
| fork safety | A store built before `fork()` (gunicorn `--preload`) is used in every worker. Do not share sockets, threads or connections across the fork: reopen them in the child (`os.register_at_fork(after_in_child=...)`, or compare `os.getpid()` on use), as `SQLiteStore` does |

Useful helpers: `Filters.from_params(...)` parses and validates raw query parameters
(raising `FilterError`), `parse_window`, `percentile`, and `encode_cursor` /
`decode_cursor` live in `redline_core.storage`.

### Plugging it in

=== "FastAPI"

    ```python
    install(app, engine=engine, store=MyStore())
    ```

=== "Django"

    ```python
    REDLINE = {"STORE": "myproject.redline.MyStore"}   # called with the core Settings
    ```

    `STORE` takes a `Store` instance, a callable or class taking the core `Settings`, or
    the dotted path of either. See [Django: custom store](django.md#custom-store).

### Building on `SQLiteStore`

The default store is a good base class: subclass it and override `save` to forward to
another system as well, or call `SQLiteStore.from_settings(settings, ...)` and wrap it:

```python
from redline_core.storage import SQLiteStore

class MirroringStore(SQLiteStore):
    def save(self, trace):
        super().save(trace)
        forward_to_elsewhere(trace.to_dict(include_profile=False))   # must also not block
```

## Using the core directly

Everything the adapters use is public in `redline_core`, so you can instrument something
Redline does not cover (a Celery task, a management command, another framework):

```python
from redline_core import Recorder, Settings, start_trace, finish_trace, record_query
from redline_core.storage import SQLiteStore

settings = Settings.from_mapping({"storage_path": ".redline/jobs.db"})
recorder = Recorder(settings, SQLiteStore.from_settings(settings))

builder = recorder.start_trace(method="JOB", path="/jobs/send-email", framework="other")
try:
    ...                                   # your hooks call record_query(...) / record_span(...)
    recorder.finish_trace(builder, status=200)
except Exception as exc:
    recorder.finish_trace(builder, status=500, error=exc)
    raise
```

| Function | Purpose |
|---|---|
| `start_trace(...)` | begin a trace in the current context; pass **unredacted** headers, query and body. Redaction happens at finish |
| `finish_trace(builder, status=..., headers=, body=, size=, route=, view=, error=)` | end it; always returns the `Trace`, which is stored if sampling keeps it |
| `record_query(sql, params, *, start, duration, db_alias, vendor, rows, many)` | record SQL. `start` is a `time.perf_counter()` reading and `duration` is in **seconds** |
| `record_span(kind, name, start, duration, data)` | record any other span, same units |
| `current()` | the active `TraceBuilder`, or `None` |
| `span(...)` | [custom spans](custom-spans.md) |

`recorder.recording(**fields)` is a context manager that starts a trace, finishes it with
status 200, or 500 if the block raises, and leaves the result in `recorder.last_trace`,
which is handy in tests.

!!! note "No public stability promise yet"

    The adapters use exactly these functions, so they work, but until 1.0 the signatures
    can change. Pin the version.
