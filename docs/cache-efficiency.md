# Cache efficiency

[← Index](index.md)

A cache that nobody checks tends to rot: keys that always miss, values written and never
read, a hot key that expires and sends fifty requests to the database at once. The
**Cache** page looks across recorded requests for those three things.

## Recording

| Source | How |
|---|---|
| Django cache framework (any backend) | recorded by `django-redline` automatically |
| redis-py, sync and asyncio | [`instrument_redis()`](integrations.md#redis) |

Each operation is a `cache` span with `op`, `key`, `backend` and, for reads, `hit`.

For the aggregate, keys are reduced to **patterns**: digit runs, UUIDs and hex strings of
eight or more characters become `*`, but digits glued to a preceding letter or digit are
kept. So `user:42:profile` and `user:77:profile` are both `user:*:profile`, while `v2`
stays `v2`.

Which commands count:

| Class | Commands |
|---|---|
| Reads | `get`, `get_many`, `get_or_set`, `has_key`, `mget`, `hget`, `hmget`, `hgetall`, `exists`, `getex`, `getdel` (and Django's async `a...` variants) |
| Writes | `set`, `add`, `set_many`, `setex`, `psetex`, `setnx`, `mset`, `hset`, `hmset`, `incr`, `decr`, `incrby`, `decrby` |
| Deletes | `delete`, `delete_many`, `del`, `unlink`, `hdel`, `clear` |

Spans with no key (a Redis pipeline is one keyless span) are skipped.

## The page

One row per backend and key pattern:

| Column | Meaning |
|---|---|
| Gets, hits, misses | reads, and how they resolved |
| Hit rate | `hits / (hits + misses)`. Blank when the backend does not say (a plain `EXISTS`) |
| Sets, deletes | writes and invalidations |
| Unread sets | writes to a pattern that had **no reads at all** in the window |
| Stampedes | bursts of three or more requests that missed the **same pattern within one second** |

### Reading it

- **Low hit rate on a hot pattern.** The TTL is too short, the key includes something that
  varies (a timestamp, a random id), or the data is never reused. A pattern that is read
  often and hits rarely is wasted round trips.
- **Unread sets.** You pay to compute and store a value that nothing ever fetched. Delete
  the write, or find the reader that uses a different key spelling.
- **Stampedes.** The classic *thundering herd*: a popular key expires and every concurrent
  request recomputes it. Add a lock around the recompute, stagger expirations with jitter,
  refresh before expiry, or serve stale while one worker refreshes.

## API

`GET api/cache?window=` returns the same rows. See [HTTP API](http-api.md#get-apicache).

## Limits

- Statistics come from *kept* traces. With `sample_rate = 0.1` they describe a 10% sample;
  rates are still meaningful, absolute counts are not.
- Stampede detection uses request **start times** and a one-second window, so it can miss
  a stampede spread over several seconds and can flag requests that merely arrived together.
- A key built from a short alphanumeric id (`user:ab12`) is not collapsed, so such patterns
  stay separate rows.
