# Custom spans

[← Index](index.md)

Redline records SQL, cache, template and outbound HTTP automatically. Everything else
your code does, a pricing calculation, an image resize, a call to a library Redline does
not know about, is invisible until you wrap it in a **span**. It then appears in the
timeline, nested under whatever contained it, with its own duration and any data you
attach.

`span` is the **only** part of Redline your own code needs to touch.

```python
import redline_core
```

Django re-exports it: `from django_redline import span`.

## Three ways to use it

=== "Decorator"

    ```python
    import redline_core

    @redline_core.span("pricing.compute")
    def compute_price(order):
        ...

    @redline_core.span                      # bare: named after the function
    def apply_discounts(order): ...

    @redline_core.span()                    # same, with parentheses
    async def fetch_rates(): ...            # coroutine functions work too
    ```

=== "Context manager"

    ```python
    with redline_core.span("render-pdf", pages=3):
        pdf = render(document)
    ```

=== "Async context manager"

    ```python
    async with redline_core.span("fetch-rates", provider="ecb"):
        rates = await client.get(url)
    ```

## Signature

```python
span(name: str | None = None, kind: SpanKind = "custom", **data) -> SpanContext
```

| Argument | Meaning |
|---|---|
| `name` | The label shown in the timeline. When omitted on a decorator, the function's `__qualname__` is used; on a `with` block, `"span"` |
| `kind` | One of `sql`, `cache`, `http_out`, `template`, `middleware`, `view`, `custom`. Default `custom` (pink in the timeline) |
| `**data` | Extra keyword arguments become the span's `data`, shown in the span drawer. Values are made JSON-safe (long strings are cut, objects become `str()`) |

The returned object has a `span_id` property (the id of the open span, or `None` when
nothing is being recorded). If the block raises, the span is recorded with
`data["error"]` set to the exception class name, and the exception propagates untouched.

## Nesting

Spans nest through `contextvars`, so a span opened inside another becomes its child, in
async code and threads as well:

```python
@redline_core.span("checkout")
def checkout(cart):
    with redline_core.span("validate"):
        validate(cart)
    with redline_core.span("charge", provider="stripe"):
        charge(cart)          # SQL run here appears under "charge"
```

SQL, cache and outbound HTTP spans recorded inside a custom span are its children, which
is the quickest way to see "which part of my function issued these 12 queries".

## Cost, and outside a request

- Outside a recorded request (a management command, a test, a request that was ignored,
  or after the trace finished) `span` does **nothing** and costs one `ContextVar.get()`.
  You can leave it in permanently, even in libraries.
- Inside a request it costs two `perf_counter()` calls, a span object and a lock to
  append it. Do not wrap a function that runs a million times per request; wrap the unit
  of work you want to see.
- It does not capture a callsite or SQL. It is a timer with a name.

## A span instance cannot be shared

One `SpanContext` must not be entered concurrently with itself. Decorator form is safe
everywhere (a fresh context is created per call); with the context-manager form, create it
inside the code that uses it rather than as a module-level constant:

```python
# do not do this
BIG = redline_core.span("big")      # shared, entered from many threads
...

# do this
with redline_core.span("big"):
    ...
```

## Tagging the whole request

To attach information to the **trace** rather than a span, use the builder:

```python
from redline_core import current

builder = current()
if builder is not None:
    builder.tag("tenant", "acme")
```

Tags are strings, show as pills in the **Request / Response** tab, and can be filtered
with `GET api/traces?tag=tenant:acme`. `current()` returns `None` outside a recorded
request.
