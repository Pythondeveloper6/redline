# Logs and exceptions

[← Index](index.md)

A slow or failing request is a story, and the SQL timeline is only half of it. The other
half is what your own code said while it ran: the `logger.warning("retrying payment")` two
lines before the 500, and the value of `order` at the moment it raised. Redline now keeps
both with the trace.

## Logs

Every record your application logs **during a recorded request** is filed under that
request. Open a request and choose the **Logs** tab.

```text
  +12.4 ms  INFO     shop.cart       Loaded cart for user 42            shop/cart.py:31
  +96.0 ms  WARNING  shop.payments   Gateway slow (retry 1)             shop/payments.py:77
 +311.8 ms  ERROR    shop.payments   Gateway timeout, giving up         shop/payments.py:84
```

Each row has the time since the request started, the level, the logger name, the message
and the file and line of the log call (project-relative, clickable if
[open in editor](the-ui.md#open-in-editor) is configured).

### How it is captured

Redline does not add a logging handler or change any logger's level. The first recorder
with `capture_logs` wraps the log record factory (`logging.setLogRecordFactory`, keeping
whatever factory was installed before), so your own `logging.basicConfig(...)`,
`dictConfig(...)` or `basicConfig(force=True)` work exactly as without Redline, before or
after it is configured. Each record is filed under whichever request is active in the
current `contextvars` context, so concurrent requests and threads do not mix. Outside a
request a record costs a single `ContextVar.get()`. Records from Redline's own `redline`
loggers are ignored so it can never recurse.

Python only creates a record when the logger is enabled for its level, so capture follows
**your** logging levels and `log_level` is an additional minimum on top. To capture INFO
your loggers must be at INFO: Django's default `LOGGING` and most configured apps are. An
app that never configured logging has its root logger at WARNING, so only WARNING and above
are captured there (call `logging.basicConfig(level=logging.INFO)` to get INFO too).

| Setting | Default | Meaning |
|---|---|---|
| `capture_logs` | `True` | Master switch |
| `log_level` | `"INFO"` | An extra minimum level for stored records, on top of your loggers' own levels. `"WARNING"` is a good production value |
| `max_logs_per_trace` | `200` | Cap per request. Extras are counted in `logs_dropped` and the UI says how many were dropped |

Messages are formatted, **redacted** and capped at 2,000 characters. Redaction looks for
`key=value`, `key: value` and `"key": "value"` pairs whose key matches `redact_keys`
(`password=...`, `"token": "..."`) and masks the value; passwords inside URLs
(`https://user:password@host`) and secret query-string values are masked too. It is the same key-based rule as everywhere else,
so a secret that appears bare in a sentence ("the code is 4821") is **not** masked. See
[Security](security.md#logs-and-locals).

!!! tip "Logs in the trace list"

    The **Logs** tab on a request shows only records from that request. To find requests
    that logged a particular problem, open the request that raised it and follow the
    linked issue; logs are not searchable across requests (that is a job for your log
    pipeline).

## Exceptions with locals

When a request raises an unhandled exception, the trace's `error` has always held the type,
message and traceback. With `capture_locals` (on by default) it also keeps the **project
frames** of the traceback together with the local variables in each, which is usually the
fastest way to see *why*:

```text
OrderError: no stock for sku 'A-77'
  shop/services.py:58  in reserve
      item   = <OrderItem 913: sku='A-77' qty=3>
      stock  = <Stock sku='A-77' on_hand=2>
      order  = <Order 4410 status='pending'>
  shop/views.py:21  in checkout
      request = <WSGIRequest: POST '/checkout/'>
      cart    = {'A-77': 3, 'B-10': 1}
```

Open the request and choose the **Exception** tab: each frame is expandable, with the
source line, the variables, and an open-in-editor link.

### What is kept

| Limit | Value |
|---|---|
| Frames | project code only (library frames are not captured), innermost last, at most 20 |
| Locals per frame | at most 30 |
| Each value | its `repr`, truncated at 300 characters |
| Names starting `__` | skipped |
| Unprintable objects | shown as `<unrepresentable>` |

### What is masked

A local whose **name** matches `redact_keys` (`password`, `token`, `api_key`, `session`,
`card`, ...) is replaced by `[redacted]`. A local that is a dict or other mapping has its
secret-looking **keys** masked before it is printed.

This is key-based, so it has the same blind spot as the rest of redaction: a secret held in
a variable with an innocent name (`x = request.headers["Authorization"]`), or inside a
model's `repr`, is shown as it is. Locals can contain personal data too: an `order`
object's `repr` may include a customer's name or email. If that is a concern,
turn the feature off and keep the traceback:

```python
REDLINE = {"CAPTURE_LOCALS": False}
```

The `production` [preset](configuration.md#presets) does exactly that. The `locals` and `logs`
[feature switches](access-control.md#feature-switches) turn the two features off from the
access-control side.

### Settings

| Setting | Default |
|---|---|
| `capture_locals` | `True` |

## Cost

Logs: one `ContextVar.get()` per record outside a request; inside, building one small
object (message formatting, redaction and a callsite lookup). Locals are captured **only
when a request raises**, from the traceback, so the happy path pays nothing.

## Correlating your own logs and error reports

Redline's recordings, your log files and your error tracker can all point at the same
request through the Redline trace id.

```python
import logging
from redline_core.correlation import TraceIdFilter

handler = logging.StreamHandler()
handler.addFilter(TraceIdFilter())          # a handler filter sees every record
handler.setFormatter(logging.Formatter("%(asctime)s [%(redline_trace_id)s] %(message)s"))
logging.getLogger().addHandler(handler)
```

`redline_trace_id` is the id of the request or job being recorded, `-` outside one. The
filter never drops a record. `install_log_filter()` adds it to the root logger's existing
handlers. Search the id in the UI (`/_redline/#/requests/<id>`).

**Sentry.** Without importing anything from Sentry at module level, Redline can tag events
that happen inside a recording and add an "Open in Redline" link:

```python
import sentry_sdk
from redline_core.correlation import chain_before_send

sentry_sdk.init(
    dsn=...,
    before_send=chain_before_send(None, base_url="https://dev.example.com"),
)
```

The event gets the tag `redline_trace_id` and a `redline` context with `trace_id` and
`open_in_redline`. `base_url` defaults to `$REDLINE_PUBLIC_URL`; pass `mount="..."` when the UI
is not at `/_redline`. Pass your existing hook as the first argument to keep it.
`tag_sentry_scope()` sets the same tag and context on the current scope instead (a no-op when
`sentry_sdk` is not installed). Remember that Redline is for development and staging: only
link to a UI that exists where the event happens.

**OpenTelemetry.** Exported spans (`redline export --send`) carry the attribute
`redline.trace_id`, next to the W3C trace id, so a collector can link back to the recording.
