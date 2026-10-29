# Blocking in async

[← Index](index.md)

An `async def` handler runs on the event loop, one thread shared by every request. A
synchronous call inside it, `time.sleep`, `requests.get`, a sync ORM query, a big
`json.dumps`, does not just slow *that* request. It stops the loop, so every other
in-flight request waits too. It is the most common way to make an async service slow, and
it is invisible in a profiler that only times one request at a time.

Redline detects it and names the call.

## What you see

An issue of kind `blocking_in_async`:

```text
Event loop blocked 412 ms by requests.get   (shop/views.py:57)
```

with a `blocking` span in the Timeline at the moment of the stall, and the calling line as
the callsite. Spans with the same call and callsite are one issue, so `time.sleep` in a
loop is reported once with its count and total.

| Severity | When |
|---|---|
| `warning` | a stall of at least `blocking_ms` (default 20 ms) |
| `critical` | a stall at five times that or more |

## How it works

Both mechanisms are active **only while at least one request is in flight** on a loop. An
idle process pays nothing.

1. **A lag monitor.** A timer re-arms itself every 5 ms on the event loop
   (`loop.call_later`, no task) and measures how late it fires. A tick that is late by
   `blocking_ms` or more means the loop was stalled. The stall becomes a `blocking` span
   with `reason = "loop_lag"`.
2. **A watchdog thread.** One per process. When a tick is overdue by half of
   `blocking_ms`, it samples the loop thread's stack with `sys._current_frames()`. That
   sample gives the stall its callsite (the innermost project frame), the blocking call
   (`time.sleep`, `requests.get`, `sync ORM`, ...) and, through the running task, the
   request it belongs to.
A sample without a project frame keeps the culprit unknown. A sampled SQLAlchemy
   frame is labeled `SQLAlchemy work`: compilation/materialization can block even with
   an async driver, so the sample alone does not establish synchronous database I/O.
3. **Sync driver hooks.** When a synchronous database driver is called on the loop thread
   itself (a sync ORM query inside an `async def` view), the SQL hook marks it as a
   `blocking` span with `reason = "sync_call"`, so you see "sync ORM" even for a query that
   was individually fast.

If the culprit cannot be told apart from the other requests in flight, the stall is
attributed to every request that was running on that loop at the time, because each of
them really was blocked.

A stall that is still in progress when the request finishes (`time.sleep(1)` followed by a
`return`) is flushed when the trace is finished, so it is not lost.

## Fixing it

| Blocking call | Fix |
|---|---|
| `time.sleep(n)` | `await asyncio.sleep(n)` |
| `requests.get(...)` | `httpx.AsyncClient`, or `await asyncio.to_thread(requests.get, ...)` |
| a sync ORM or driver call | the async driver (`asyncpg`, `aiosqlite`), `AsyncSession`, or `sync_to_async` / `run_in_threadpool` |
| CPU-heavy work (hashing, parsing, image resize) | `await asyncio.to_thread(...)` or a process pool |
| a threading lock held on the loop | restructure; do not block the loop on a lock |

A plain `def` handler in FastAPI and Litestar (`sync_to_thread=True`) runs in a worker
thread, not on the loop, so it cannot cause this issue.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `detect_blocking` | `True` | Run the monitor in async adapters. Set it false to remove the monitor entirely |
| `blocking_ms` | `20` | Minimum stall that is recorded and reported |

The overhead is the timer callback, a few microseconds every 5 ms per busy loop. See
[Performance](performance.md).

## Which adapters

| Adapter | Behaviour |
|---|---|
| FastAPI | Lag monitor, watchdog and sync-driver hooks |
| Django on ASGI | Lag monitor and the sync ORM hook, for `async def` views |
| Litestar | Lag monitor (the app runs inside it while a request is recorded) |
| Django on WSGI, Flask | No event loop to watch: not applicable |

## Limits

- The lag monitor measures a loop that is *late*, which is the right signal but is not proof
  of a specific line. The sampled stack is the best available evidence.
- A stall shorter than `blocking_ms` is invisible by design. Lower the setting to see
  smaller ones, at the cost of more noise.
- Work in C extensions that release the GIL is not a stall; work that holds it is.
